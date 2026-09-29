#!/usr/bin/env python3
"""Add one Balltime game to King of the Court's Progress tab.

Usage:
  python3 tools/ingest_balltime.py <balltime link or video id> [--name NAME] [--opponent TEXT]
  python3 tools/ingest_balltime.py --from-file saved-videos-metadata.json [...]

What it does
  * Makes the same single unauthenticated request the public Balltime share page makes:
      POST https://backend.balltime.com/videos-metadata  {"video_ids": [<id>], ...}
    No login, no cookies, no tokens, and never the paid generate-multi-video-stats endpoint.
  * Uses only visible actions (hide_until_verified excluded), like the share page.
  * Finds Leo (player name "Leo", or jersey 5) and treats his team as team A. If Leo is on
    team b the whole game is flipped so team A is always Leo's team.
  * Writes data/games/<date>-<shortid>.json (compact: match meta, team A box score, Leo's
    setting chains, precomputed metrics) and upserts data/games/index.json keyed by the
    Balltime video id, so running it twice for the same game just refreshes it.
Only the Python standard library is used.
"""
import argparse, collections, json, os, re, sys, urllib.request, urllib.error

API = 'https://backend.balltime.com/videos-metadata'
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GAMES_DIR = os.path.join(ROOT, 'data', 'games')
UUID_RE = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', re.I)
LEO_NAME, LEO_JERSEY = 'Leo', 5
W, L = 9.0, 18.0            # court width, full length (m)
QUICK_AIR_S = 0.9           # front-row set with Balltime air_time <= this = quick tempo
QUICK_CALLS = {'1', 'push', 'blue'}   # only used when air_time is missing
SWING = ('kill', 'error', 'in_play')

def die(msg, code=1):
    print('ingest_balltime: ' + msg, file=sys.stderr); sys.exit(code)

def video_id(arg):
    m = UUID_RE.search(arg or '')
    if not m: die(f'no Balltime video id in {arg!r} (expected https://app.balltime.com/video/<uuid>)')
    return m.group(0).lower()

def fetch(vid):
    body = json.dumps({'video_ids': [vid], 'withTeamData': False, 'dev': False, 'excludeAnalystEditsFromDev': False}).encode()
    req = urllib.request.Request(API, data=body, method='POST', headers={
        'Content-Type': 'application/json', 'Accept': 'application/json',
        'Origin': 'https://app.balltime.com', 'Referer': 'https://app.balltime.com/',
        'User-Agent': 'Mozilla/5.0 (KingOfTheCourt ingest)'})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.loads(r.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        die(f'Balltime answered HTTP {e.code} for {vid} (video private, deleted or not shared?)')
    except urllib.error.URLError as e:
        die(f'could not reach Balltime: {e.reason}')
    except ValueError:
        die('Balltime response was not JSON')
    if not isinstance(data, list) or not data: die(f'Balltime returned no video for {vid}')
    return data[0]

# ---------- helpers ----------
def q(a): return str(a.get('quality'))
def pl(a):
    p = a.get('player') or []
    return p[0] if p and isinstance(p[0], dict) else {}
def jersey(a):
    j = pl(a).get('jersey_number')
    return None if j is None else str(j)
def is_leo(p): return bool(p) and (p.get('name') == LEO_NAME or (p.get('jersey_number') == LEO_JERSEY and p.get('team') in ('a', 'b', None)))
def rnd(x, n=3): return None if x is None else round(x, n)
def pct(n, d): return None if not d else round(n / d, 4)
def eff(k, e, att): return None if not att else round((k - e) / att, 4)

def flip_game(v):
    sw = {'a': 'b', 'b': 'a'}
    for r in v['video_analysis']:
        for k in ('serving_team', 'team_won'):
            if r.get(k) in sw: r[k] = sw[r[k]]
        for a in r['actions']:
            if a.get('team') in sw: a['team'] = sw[a['team']]
            for p in (a.get('player') or []) + (a.get('setter') or []):
                if isinstance(p, dict) and p.get('team') in sw: p['team'] = sw[p['team']]
    sc = v.get('score') or {}
    for s in sc.get('sets', []): s['a'], s['b'] = s.get('b'), s.get('a')
    sc['team_a_sets_won'], sc['team_b_sets_won'] = sc.get('team_b_sets_won'), sc.get('team_a_sets_won')

def to_m(pt, far):
    """Balltime positional [x, y]: x 0..1 across, y 0 = far baseline, .5 = net, 1 = near baseline.
    Returns [x across from the team's left sideline, d = metres off the net] from that team's side."""
    if not pt or len(pt) < 2 or pt[0] is None or pt[1] is None: return None
    x, y = pt[0], pt[1]
    if far: x, y = 1 - x, 1 - y
    return [round(x * W, 2), round((y - 0.5) * L, 2)]

def destination(set_src, set_dst, air, call):
    """zone of the attack contact point, relative to Leo when he set it (setter faces the left pin)."""
    if not set_dst: return None, None
    x, d = set_dst
    if d > 3.0: return 'back-row', False
    quick = (air is not None and air <= QUICK_AIR_S) or (air is None and call in QUICK_CALLS)
    if set_src and x > set_src[0] + 1.0: return 'behind-setter', quick
    if x < 3.0: return 'left', quick
    if x <= 6.0: return 'middle', quick
    return 'right', quick

def bucket(ch):
    if ch['result'] in ('free', 'none'): return 'not-attacked'
    z = ch['zone']
    if z is None: return 'unknown'
    if ch['quick'] or z == 'middle': return 'middle/quick'
    if z in ('behind-setter', 'right'): return 'right/back-set'
    if z == 'left': return 'left'
    return 'back-row'

# ---------- main build ----------
def build(v, name=None, opponent=None):
    va = v.get('video_analysis') or []
    if not va: die('this video has no rallies/actions (not processed yet?)')
    allacts = [a for r in va for a in r['actions']]
    leo_teams = collections.Counter(a.get('team') for a in allacts if not a.get('hide_until_verified') and is_leo(pl(a)))
    if not leo_teams: die('no action tagged to Leo (name "Leo" / jersey 5) in this video; not adding it')
    leo_team = leo_teams.most_common(1)[0][0]
    warnings = []
    if len([t for t in leo_teams if t]) > 1:
        warnings.append(f'Leo appears on more than one team {dict(leo_teams)}; used the majority team {leo_team!r}.')
    if leo_team not in ('a', 'b'): die(f'Leo is on an unassigned team ({leo_team!r}); team side ambiguous, not adding it')
    flipped = leo_team == 'b'
    if flipped: flip_game(v); warnings.append('Leo was on Balltime team b; flipped so team A = Leo\'s team.')
    vis = lambda r: [a for a in r['actions'] if not a.get('hide_until_verified')]
    acts = [a for r in va for a in vis(r)]
    has_pos = any(a.get('positional') for a in acts)

    # ---- match meta ----
    sc = v.get('score') or {}
    sets_sc = [{'set': i + 1, 'a': s.get('a'), 'b': s.get('b')} for i, s in enumerate(sc.get('sets', []))]
    wa, wb = sc.get('team_a_sets_won'), sc.get('team_b_sets_won')
    res = None if wa is None or wb is None else ('W' if wa > wb else 'L' if wb > wa else 'T')
    so = lambda t: {'won': sum(1 for r in va if r.get('serving_team') not in (t, None) and r.get('team_won') == t),
                    'of': sum(1 for r in va if r.get('serving_team') not in (t, None))}
    meta = {'setScores': sets_sc, 'setsWon': {'a': wa, 'b': wb}, 'result': res,
            'resultText': (f"{res} {wa}–{wb} (" + ', '.join(f"{s['a']}–{s['b']}" for s in sets_sc) + ')') if res else None,
            'sideOut': {t: so(t) for t in 'ab'}, 'rallies': len(va),
            'pointsWon': {t: sum(1 for r in va if r.get('team_won') == t) for t in 'ab'}}

    # ---- team A box score ----
    def box(sub, label, j):
        sv = [a for a in sub if a['skill_type'] == 'serve']; rc = [a for a in sub if a['skill_type'] == 'receive']
        st = [a for a in sub if a['skill_type'] == 'set']; at = [a for a in sub if a['skill_type'] == 'attack']
        rated = [int(q(a)) for a in rc if q(a) in ('0', '1', '2', '3')]
        k = sum(q(a) == 'kill' for a in at); e = sum(q(a) == 'error' for a in at)
        return {'player': label, 'jersey': j, 'sets': len(st), 'assists': sum(q(a) == 'assist' for a in st),
                'attacks': len(at), 'kills': k, 'attackErrors': e, 'hittingPct': eff(k, e, len(at)),
                'serves': len(sv), 'aces': sum(q(a) == 'ace' for a in sv), 'serveErrors': sum(q(a) == 'error' for a in sv),
                'receptions': len(rc), 'ratedReceptions': len(rated), 'passRating': rnd(sum(rated) / len(rated), 3) if rated else None,
                'digs': sum(a['skill_type'] == 'dig' for a in sub)}
    ta = [a for a in acts if a.get('team') == 'a']
    groups = collections.defaultdict(list)
    for a in ta: groups[jersey(a)].append(a)
    players = []
    for j in sorted(groups, key=lambda j: (j is None, int(j) if j and j.isdigit() else 999)):
        label = ('Leo' if any(is_leo(pl(a)) for a in groups[j]) else f'#{j}') if j else 'No jersey'
        players.append(box(groups[j], label, j or ''))
    team_totals = {t: box([a for a in acts if a.get('team') == t], f'Team {t.upper()}', '') for t in 'ab'}

    # ---- Leo's setting chains ----
    chains = []
    for r in va:
        ac = vis(r)
        for i, a in enumerate(ac):
            if a['skill_type'] != 'set' or a.get('team') != 'a' or not is_leo(pl(a)): continue
            prev = ac[i - 1] if i else None
            rating = None
            if prev is None: origin = 'other'
            elif prev.get('team') != 'a': origin = 'overpass'
            elif prev['skill_type'] == 'receive':
                origin = 'serve-receive'; rating = int(q(prev)) if q(prev) in ('0', '1', '2', '3') else None
            elif prev['skill_type'] == 'dig': origin = 'dig'
            elif prev['skill_type'] == 'free_ball_received': origin = 'free'
            else: origin = 'scramble'
            system = ('in' if rating >= 2 else 'oos') if rating is not None else ('unknown' if origin == 'serve-receive' else 'oos')
            pos = a.get('positional') or {}
            far = bool(pos.get('is_player_on_far_side'))
            src, dst = to_m(pos.get('src'), far), to_m(pos.get('dest'), far)
            air = pos.get('air_time'); call = a.get('attack_type')
            result, blockers, attacker, atype = 'none', None, None, None
            for b in ac[i + 1:]:
                if b.get('team') != 'a': break
                if b['skill_type'] == 'attack':
                    result = q(b) if q(b) in SWING else 'attack-unknown'
                    blockers = b.get('num_of_blockers'); attacker = jersey(b); atype = b.get('attack_type')
                    if dst is None: dst = to_m((b.get('positional') or {}).get('src'), bool((b.get('positional') or {}).get('is_player_on_far_side')))
                    break
                if b['skill_type'] == 'free_ball': result = 'free'; break
            zone, quick = destination(src, dst, air, call)
            ch = {'rally': r.get('id'), 'set': r.get('set_number'), 'origin': origin, 'pass': rating, 'system': system,
                  'btOOS': a.get('out_of_system'), 'call': call, 'airTime': air, 'setFrom': src, 'setTo': dst,
                  'zone': zone, 'quick': quick, 'assist': q(a) == 'assist', 'result': result,
                  'attacker': attacker, 'attackType': atype, 'blockers': blockers, 'won': r.get('team_won') == 'a'}
            if atype in ('dump', 'overpass'): ch['zone'] = None
            ch['bucket'] = bucket(ch)
            chains.append(ch)
    metrics = compute_metrics(chains)
    date = (v.get('video_file_date') or '')[:10] or None
    avail = {
        'positional': has_pos, 'setCalls': any(c['call'] for c in chains),
        'passRatings': any(c['pass'] is not None for c in chains),
        'blockers': any(c['blockers'] is not None for c in chains),
        'setRating': False, 'blockPositions': False, 'blockActions': False,
    }
    notes = ['Balltime share-page data, AI-tagged (stats_status: %s).' % v.get('stats_status'),
             'Balltime has no set rating/grade field: a set\'s quality is only assist / in_play / unknown.',
             'Blocking comes only from num_of_blockers on each attack (0-3). There are no blocker positions, ids or block types; '
             'visible block actions: %d.' % sum(a['skill_type'] == 'block' for a in acts)]
    if not has_pos: notes.append('No positional data for this video (positional_data_status: %s), so set destinations and quick tempo are unknown.' % v.get('positional_data_status'))
    if not avail['passRatings']: notes.append('Balltime left every reception before Leo\'s sets unrated, so in-system vs out-of-system is only known for dig/free/scramble sets.')
    if not avail['setCalls']: notes.append('No Balltime set calls (attack_type) in this video.')
    game = {'type': 'kotc-game', 'version': 1, 'id': v['id'], 'name': name or v.get('name'), 'date': date,
            'opponent': opponent, 'link': f"https://app.balltime.com/video/{v['id']}",
            'source': 'POST backend.balltime.com/videos-metadata (public share-page request), visible actions only',
            'flipped': flipped, 'warnings': warnings, 'available': avail, 'notes': notes,
            'actions': {'visible': len(acts), 'hidden': len(allacts) - len(acts)},
            'meta': meta, 'players': players, 'teamTotals': team_totals,
            'chains': chains, 'metrics': metrics}
    return game

def grp(cs):
    att = [c for c in cs if c['result'] in SWING]
    k = sum(c['result'] == 'kill' for c in att); e = sum(c['result'] == 'error' for c in att)
    atk = [c for c in cs if c['result'] not in ('free', 'none')]
    return {'n': len(cs), 'attacked': len(atk), 'attackedPct': pct(len(atk), len(cs)), 'swings': len(att),
            'kills': k, 'errors': e, 'killPct': pct(k, len(att)), 'eff': eff(k, e, len(att)),
            'assists': sum(c['assist'] for c in cs), 'assistPct': pct(sum(c['assist'] for c in cs), len(cs))}

def blockers(cs):
    att = [c for c in cs if c['result'] in SWING]
    known = [c for c in att if c['blockers'] is not None]
    dist = {str(b): sum(c['blockers'] == b for c in known) for b in range(4)}
    sep = [c for c in known if c['blockers'] <= 1]
    by = {}
    for b in range(4):
        g = [c for c in known if c['blockers'] == b]
        by[str(b)] = grp(g)['eff'] if g else None
    return {'attacks': len(att), 'known': len(known), 'dist': dist, 'effBy': by,
            'separated': len(sep), 'separatedOfSets': pct(len(sep), len(cs)), 'separatedOfAttacks': pct(len(sep), len(known)),
            'effSeparated': grp(sep)['eff'], 'effTwoPlus': grp([c for c in known if c['blockers'] >= 2])['eff']}

BUCKETS = ['left', 'right/back-set', 'middle/quick', 'back-row', 'not-attacked', 'unknown']

def compute_metrics(chains):
    m = {}
    m['quality'] = grp(chains)
    ins = [c for c in chains if c['system'] == 'in']; oos = [c for c in chains if c['system'] == 'oos']
    unk = [c for c in chains if c['system'] == 'unknown']
    m['system'] = {'in': len(ins), 'oos': len(oos), 'unknown': len(unk), 'oosShare': pct(len(oos), len(chains)), 'oosShareKnown': pct(len(oos), len(ins) + len(oos)),
                   'effIn': grp(ins)['eff'], 'effOos': grp(oos)['eff'], 'effUnknown': grp(unk)['eff'],
                   'origins': dict(collections.Counter(c['origin'] for c in chains)),
                   'btOOSFlagged': sum(1 for c in chains if c['btOOS'] is True)}
    kd = [c for c in chains if c['zone'] is not None]
    beh = [c for c in kd if c['zone'] == 'behind-setter']
    m['back'] = {'known': len(kd), 'behind': len(beh), 'share': pct(len(beh), len(kd)), 'eff': grp(beh)['eff']}
    mid = [c for c in kd if c['zone'] == 'middle' or c['quick']]
    kin = [c for c in ins if c['zone'] is not None]; midin = [c for c in kin if c['zone'] == 'middle' or c['quick']]
    m['quick'] = {'known': len(kd), 'middle': len(mid), 'share': pct(len(mid), len(kd)), 'eff': grp(mid)['eff'],
                  'quicks': sum(1 for c in kd if c['quick']), 'inSysKnown': len(kin), 'inSysMiddle': len(midin),
                  'inSysShare': pct(len(midin), len(kin))}
    b = blockers(chains)
    b['byBucket'] = {}
    for k in BUCKETS:
        g = [c for c in chains if c['bucket'] == k]
        if g and k != 'not-attacked':
            bb = blockers(g); b['byBucket'][k] = {'attacks': bb['known'], 'separated': bb['separated'], 'separatedOfAttacks': bb['separatedOfAttacks'], 'eff': grp(g)['eff']}
    m['blockers'] = b
    def pressure(cs):
        d = grp(cs); bl = blockers(cs)
        d['blockers'] = bl['dist']; d['beatMiddle'] = bl['separated']; d['beatMiddlePct'] = bl['separatedOfSets']
        d['beatMiddleOfAttacks'] = bl['separatedOfAttacks']
        d['dest'] = {k: sum(c['bucket'] == k for c in cs) for k in BUCKETS}
        return d
    bad = [c for c in chains if c['origin'] == 'serve-receive' and c['pass'] is not None and c['pass'] <= 1]
    scr = [c for c in chains if c['origin'] in ('dig', 'scramble', 'overpass')]
    m['badPass'] = {'badPass': pressure(bad), 'digScramble': pressure(scr), 'combined': pressure(bad + scr),
                    'unratedReceive': sum(1 for c in chains if c['origin'] == 'serve-receive' and c['pass'] is None)}
    m['decision'] = decision(chains, ins, oos)
    return m

def decision(chains, ins, oos):
    comps = []
    kin = [c for c in ins if c['zone'] is not None]
    if len(kin) >= 3:
        s = sum(1 for c in kin if c['zone'] == 'middle' or c['quick']) / len(kin)
        comps.append({'key': 'inSysMiddle', 'label': 'Middle/quick use on in-system balls', 'value': round(s, 4),
                      'score': round(min(1, s / 0.30) * 100), 'rule': '30% or more of in-system sets to the middle/quick = 100', 'n': len(kin)})
    else:
        comps.append({'key': 'inSysMiddle', 'label': 'Middle/quick use on in-system balls', 'score': None,
                      'rule': 'needs 3+ in-system sets with a known destination', 'n': len(kin)})
    sw = [c for c in chains if c['result'] in SWING and c['attacker']]
    by = collections.defaultdict(list)
    for c in sw: by[c['attacker']].append(c)
    cand = {j: grp(cs) for j, cs in by.items() if len(cs) >= 3}
    if cand and sw:
        best = max(cand, key=lambda j: (cand[j]['eff'], cand[j]['kills']))
        s = len(by[best]) / len(sw)
        comps.append({'key': 'feedBest', 'label': f'Feeding the best hitter (#{best}, {cand[best]["eff"]:+.3f} off your sets)', 'value': round(s, 4),
                      'score': round(min(1, s / 0.35) * 100), 'rule': "share of your attacked sets to the game's most efficient hitter (3+ swings); 35% or more = 100", 'n': len(sw), 'best': best})
    else:
        comps.append({'key': 'feedBest', 'label': 'Feeding the best hitter', 'score': None, 'rule': 'needs a hitter with 3+ swings off your sets', 'n': len(sw)})
    opp = rep = 0
    for i, c in enumerate(sw):
        if c['result'] != 'error': continue
        nxt = next((d for d in sw[i + 1:] if d['set'] == c['set']), None)
        if not nxt: continue
        opp += 1
        if nxt['attacker'] == c['attacker'] and nxt['result'] == 'error': rep += 1
    comps.append({'key': 'noRepeat', 'label': 'Not feeding repeat errors', 'value': None if not opp else round(1 - rep / opp, 4),
                  'score': None if not opp else round(100 * (1 - rep / opp)), 'n': opp, 'repeats': rep,
                  'rule': 'after an attack error, the next attacked set went to the same hitter and was another error = a repeat; 100 × (1 − repeats / chances)'})
    ko = [c for c in oos if c['zone'] is not None]
    if len(ko) >= 3:
        s = sum(1 for c in ko if not (c['zone'] == 'middle' or c['quick'])) / len(ko)
        comps.append({'key': 'oosPin', 'label': 'Out-of-system balls to a pin / high ball, not the middle', 'value': round(s, 4),
                      'score': round(s * 100), 'rule': 'share of attacked out-of-system sets not sent to the middle/quick', 'n': len(ko)})
    else:
        comps.append({'key': 'oosPin', 'label': 'Out-of-system balls to a pin / high ball, not the middle', 'score': None,
                      'rule': 'needs 3+ out-of-system sets with a known destination', 'n': len(ko)})
    sc = [c['score'] for c in comps if c['score'] is not None]
    return {'score': round(sum(sc) / len(sc)) if sc else None, 'used': len(sc), 'of': len(comps), 'components': comps,
            'label': 'Heuristic, not an official rating: the average of the components that this game\'s data can support.'}

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('link', nargs='?', help='Balltime share link or video id')
    ap.add_argument('--from-file', help='use a saved videos-metadata response instead of fetching')
    ap.add_argument('--name'); ap.add_argument('--opponent')
    a = ap.parse_args()
    if a.from_file:
        d = json.load(open(a.from_file)); v = d[0] if isinstance(d, list) else d
        if a.link and video_id(a.link) != v.get('id'): die('--from-file video id does not match the link')
    else:
        if not a.link: ap.error('give a Balltime link / video id, or --from-file')
        v = fetch(video_id(a.link))
    game = build(v, a.name, a.opponent)
    os.makedirs(GAMES_DIR, exist_ok=True)
    fname = f"{game['date'] or 'undated'}-{game['id'][:8]}.json"
    with open(os.path.join(GAMES_DIR, fname), 'w') as f: json.dump(game, f, separators=(',', ':'), ensure_ascii=False)
    ip = os.path.join(GAMES_DIR, 'index.json')
    idx = json.load(open(ip)) if os.path.exists(ip) else {'type': 'kotc-games-index', 'version': 1, 'games': []}
    idx['games'] = [g for g in idx['games'] if g['id'] != game['id']]
    m = game['metrics']
    idx['games'].append({'id': game['id'], 'name': game['name'], 'date': game['date'], 'opponent': game['opponent'],
                         'result': game['meta']['resultText'], 'file': fname, 'leoSets': m['quality']['n']})
    idx['games'].sort(key=lambda g: (g['date'] or '', g['name'] or ''))
    with open(ip, 'w') as f: json.dump(idx, f, indent=1, ensure_ascii=False); f.write('\n')
    print(f"added {game['name']} ({game['date']}, {game['meta']['resultText']}) -> data/games/{fname}")
    print(f"Leo: {m['quality']['n']} sets, {m['quality']['assists']} assists; decision score {m['decision']['score']} ({m['decision']['used']}/{m['decision']['of']} parts)")
    for w in game['warnings']: print('warning: ' + w)

if __name__ == '__main__':
    main()
