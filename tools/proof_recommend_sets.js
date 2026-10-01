#!/usr/bin/env node
/**
 * Proof: recommendSets() open-pin answers are never gated by setter x.
 * Extracts the live function from index.html and asserts invariants.
 */
const fs = require('fs');
const path = require('path');
const html = fs.readFileSync(path.join(__dirname, '..', 'index.html'), 'utf8');
const m = html.match(/function recommendSets\(setterWorldX, blockerWorldXs\)\{[\s\S]*?\n\}/);
if (!m) { console.error('recommendSets not found'); process.exit(1); }
const COURT_M = { L: 18, W: 9, ATTACK: 3 };
// eslint-disable-next-line no-new-func
const recommendSets = new Function('COURT_M', m[0] + '\nreturn recommendSets;')(COURT_M);

function sameKeys(a, b) {
  return a.length === b.length && a.every((k, i) => k === b[i]);
}
function includesAll(keys, need) {
  return need.every(k => keys.includes(k));
}

const SETTERS = [-3.2, -2.0, -1.0, 0, 1.0, 2.0, 3.2]; // world x; left → right
let fails = 0;
const lines = [];

function check(name, blockers, needKeys) {
  const results = SETTERS.map(sx => ({ sx, rec: recommendSets(sx, blockers) }));
  const base = results[0].rec.keys;
  let ok = true;
  for (const r of results) {
    if (!sameKeys(r.rec.keys, base)) {
      ok = false;
      lines.push(`FAIL ${name}: setter x changed keys — sx=${r.sx} keys=${r.rec.keys} vs base=${base}`);
    }
    if (!includesAll(r.rec.keys, needKeys)) {
      ok = false;
      lines.push(`FAIL ${name}: missing ${needKeys} at sx=${r.sx} got ${r.rec.keys}`);
    }
  }
  if (ok) lines.push(`PASS ${name}: keys=${base.join('/')} stable across setter x ∈ [${SETTERS.join(', ')}]`);
  else fails++;
}

// --- Core bug cases ---
check('ALL on right (pin pack)', [2.5, 3.0, 3.5], ['shoot', '4']);
check('ALL on right (mild half)', [0.2, 0.8, 1.4], ['shoot', '4']);
check('ALL on right (barely right of mid)', [0.05, 0.4, 0.9], ['shoot', '4']);
check('ALL on left (pin pack)', [-3.5, -3.0, -2.5], ['3', '5']);
check('ALL on left (mild half)', [-1.4, -0.8, -0.2], ['3', '5']);
check('ALL on left (barely left of mid)', [-0.9, -0.4, -0.05], ['3', '5']);

// setter-independence for mass / bunched cases too
check('right mass near pin', [2.0, 2.8, 3.6], ['shoot', '4']);
check('left mass near pin', [-3.6, -2.8, -2.0], ['3', '5']);

// Prove function body does not branch on setterWorldX
const body = m[0];
if (/\bsetterWorldX\b/.test(body) && /setterWorldX\s*[+\-*/<>=]/.test(body.replace(/\/\/.*$/gm, ''))) {
  // allow only unused param mention; reject arithmetic / compare on it
  const stripped = body
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .replace(/\/\/.*$/gm, '')
    .replace(/function recommendSets\(setterWorldX,\s*blockerWorldXs\)/, 'function recommendSets(_swx, blockerWorldXs)');
  if (/\bsetterWorldX\b/.test(stripped)) {
    lines.push('FAIL: setterWorldX still referenced in function body');
    fails++;
  } else {
    lines.push('PASS: setterWorldX is unused in recommendSets body (API compat only)');
  }
} else {
  lines.push('PASS: setterWorldX is unused in recommendSets body (API compat only)');
}

console.log(lines.join('\n'));
console.log(fails ? `\nPROOF FAILED (${fails})` : '\nPROOF OK');
process.exit(fails ? 1 : 0);
