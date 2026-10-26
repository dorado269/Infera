// The board's graph (web/graph.js, from conch): edge routes that keep to the gaps and do not cross where they need not, the
// edges another path implies, and the gaps between rows that grow for the lines running along them.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { layout, edgePaths, redundant, tracks } from '../graph.js';

const task = (id, started) => ({ id, type: id.split('/')[0], status: 'done', started, created: started });
const at = (h) => `2026-10-20T${String(h).padStart(2, '0')}:00:00Z`;

// the corners of a path: its start, the corners the rounded turns go round (their Q control points) and its end
function points(d) {
  const all = [...d.matchAll(/([MLQ])(-?[\d.]+),(-?[\d.]+)/g)].map((m) => ({ c: m[1], x: +m[2], y: +m[3] }));
  return [all[0], ...all.filter((p) => p.c === 'Q'), all[all.length - 1]];
}
const segments = (d) => { const p = points(d); return p.slice(1).map((q, i) => [p[i], q]); };
function crosses([a, b], [c, d]) {
  const den = (b.x - a.x) * (d.y - c.y) - (b.y - a.y) * (d.x - c.x);
  if (!den) return false;
  const u = ((c.x - a.x) * (d.y - c.y) - (c.y - a.y) * (d.x - c.x)) / den, v = ((c.x - a.x) * (b.y - a.y) - (c.y - a.y) * (b.x - a.x)) / den;
  return u > 0.01 && u < 0.99 && v > 0.01 && v < 0.99;
}
const crossings = (paths) => {
  let n = 0;
  for (let i = 0; i < paths.length; i++) for (let j = i + 1; j < paths.length; j++)
    for (const s of segments(paths[i])) for (const r of segments(paths[j])) n += crosses(s, r);
  return n;
};
// every corner of every path is outside every card it does not start or end at
const clear = (edges, paths, pos) => edges.every((e, i) => points(paths[i]).every((p) => [...pos].every(([id, c]) =>
  id === e.from || id === e.to || !(p.x > c.x && p.x < c.x + c.w && p.y > c.y && p.y < c.y + c.h))));

test('edges are orthogonal lines in the gaps, and lines leaving the same side nest instead of crossing', () => {
  // a feeds b (next row, same lane), c (same lane, two rows down), d and e (the next lane, further down)
  const tasks = [task('x/a', at(1)), task('x/b', at(2)), task('x/c', at(3)), task('y/d', at(4)), task('y/e', at(5))];
  const edges = [{ from: 'x/a', to: 'x/b' }, { from: 'x/a', to: 'x/c' }, { from: 'x/a', to: 'y/e' }, { from: 'x/a', to: 'y/d' }];
  const L = layout(tasks, edges, ['x', 'y'], new Set());
  const paths = edgePaths(edges, L.pos);
  for (const d of paths) for (const [p, q] of segments(d)) assert.ok(p.x === q.x || p.y === q.y, `not orthogonal: ${d}`);
  assert.equal(crossings(paths), 0);
  assert.ok(clear(edges, paths, L.pos));
  // b → d now runs across the gap a's lines go down: a → c goes round the other side of the lane, a → e still crosses
  const more = [...edges, { from: 'x/b', to: 'y/d' }];
  const M = layout(tasks, more, ['x', 'y'], new Set());
  const two = edgePaths(more, M.pos);
  assert.ok(two[1].startsWith(`M${M.pos.get('x/a').x},`), two[1]);  // out of a's left side
  assert.equal(crossings(two), 1);
});

test('the order of the tracks in a gap avoids crossings the old insertion order made', () => {
  // three lines down the same lane gap from cards on the left, each turning right lower down than the one before
  const tasks = [task('x/a', at(1)), task('x/b', at(2)), task('x/c', at(3)), task('y/p', at(4)), task('y/q', at(5)), task('y/r', at(6))];
  const edges = [{ from: 'x/a', to: 'y/p' }, { from: 'x/b', to: 'y/q' }, { from: 'x/c', to: 'y/r' }];
  for (const order of [edges, [...edges].reverse()]) {
    const L = layout(tasks, order, ['x', 'y'], new Set());
    const paths = edgePaths(order, L.pos);
    assert.equal(crossings(paths), 0, paths.join('\n'));
    assert.ok(clear(order, paths, L.pos));
  }
});

test('tracks: a segment leaving the channel inside another one\'s span goes on that side of it', () => {
  // p: leaves to the left at 50, inside q's span; q: leaves to the right at 80, inside p's span? no: p is 0..60
  const p = { lo: 0, hi: 60, pins: [{ at: 0, dir: -1 }, { at: 60, dir: 1 }] };
  const q = { lo: 20, hi: 100, pins: [{ at: 20, dir: -1 }, { at: 100, dir: 1 }] };
  // q's pin at 20 (to the left/up) is inside p: q goes before p; p's pin at 60 (to the right/down) is inside q: p after q
  assert.equal(tracks([p, q]), 2);
  assert.ok(q.track < p.track);
  const far = { lo: 200, hi: 300, pins: [] };
  assert.equal(tracks([p, far]), 1);  // apart: one track for both
});

test('redundant: the edges another path of blocking edges implies; 等子任务, review and subtask edges stay', () => {
  const e = (from, to, kind = 'input') => ({ from, to, kind });
  const ab = e('a', 'b'), bc = e('b', 'c'), ac = e('a', 'c'), cd = e('c', 'd'), ad = e('a', 'd');
  assert.deepEqual([...redundant([ab, bc, ac, cd, ad])], [ac, ad]);
  const bd = e('b', 'd');
  assert.deepEqual([...redundant([ab, bc, cd, bd])], [bd]);
  assert.deepEqual([...redundant([ab, e('b', 'a')])], []);         // a cycle: nothing else leads from a to b
  // a ↔ b with c → a and c → b: one of c's lines may go, never both
  const ca = e('c', 'a'), cb = e('c', 'b');
  assert.deepEqual([...redundant([ab, e('b', 'a'), ca, cb])], [ca]);
  assert.deepEqual([...redundant([e('a', 'b', 'code'), bc, e('a', 'c', 'code')])].map((x) => x.kind), ['code']);  // code baselines too
  // 等子任务 counts as a path but is not left out: it also waits for a's subtasks
  assert.deepEqual([...redundant([e('a', 'b', 'children'), bc, ac])], [ac]);
  assert.deepEqual([...redundant([ab, bc, e('a', 'c', 'children')])], []);
  for (const kind of ['review', 'subtask']) {
    assert.deepEqual([...redundant([e('a', 'b', kind), bc, ac])], []);  // no path (they do not order the tasks)
    assert.deepEqual([...redundant([ab, bc, e('a', 'c', kind)])], []);  // nor left out
  }
});

test('layout: the gap between two rows grows when more lines run across it than fit', () => {
  // four cards in a row (lanes a-d) each feeding the card of the next row in the lane on its right: a fan of lines
  const lanes = ['a', 'b', 'c', 'd', 'e'];
  const top = lanes.slice(0, 4).map((l, i) => task(`${l}/t${i}`, at(1)));
  const below = ['e/u', 'e/v', 'e/w', 'e/x'].map((id, i) => task(id, at(2 + i)));
  const edges = top.map((t, i) => ({ from: t.id, to: 'e/u' })).concat(top.map((t) => ({ from: t.id, to: 'e/v' })));
  const L = layout([...top, ...below], edges, lanes, new Set());
  const gap = L.rows[1].y - (L.rows[0].y + L.rows[0].h);
  assert.ok(gap > 34, `gap ${gap}`);
  const paths = edgePaths(edges, L.pos);
  assert.ok(clear(edges, paths, L.pos));
});

test('two lines turning at the same place, one up and one down, do not run on top of each other', () => {
  // lanes a and b, one task a day: a0, b1, b2, a3; a0 → b2 turns down at b's middle, a3 → b1 turns up there
  const day = (d) => `2026-10-2${d}T01:00:00Z`;
  const tasks = [task('a/0', day(0)), task('b/1', day(1)), task('b/2', day(2)), task('a/3', day(3))];
  for (const edges of [[{ from: 'a/0', to: 'b/2' }, { from: 'a/3', to: 'b/1' }], [{ from: 'a/3', to: 'b/1' }, { from: 'a/0', to: 'b/2' }]]) {
    const L = layout(tasks, edges, ['a', 'b'], new Set());
    const verticals = edgePaths(edges, L.pos).map((d) => segments(d).filter(([p, q]) => p.x === q.x && p.y !== q.y)
      .map(([p, q]) => ({ x: p.x, lo: Math.min(p.y, q.y), hi: Math.max(p.y, q.y) })));
    for (const v of verticals[0]) for (const w of verticals[1]) assert.ok(v.x !== w.x || v.hi <= w.lo || w.hi <= v.lo, JSON.stringify([v, w]));
  }
});
