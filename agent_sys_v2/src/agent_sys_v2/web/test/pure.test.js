// Unit tests of the UI's pure functions: no browser, no server.  node --test web/test/*.test.js
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { statusLine, mentionAt, mentionOptions, insertMention, EDGE_KINDS, BLOCKING } from '../ui.js';
import { layout, edgePaths, laneAt, moveLane, dateRange, inDates, visibleTasks, childrenDone } from '../board.js';
import { fmt, deltaText, ticks, timeTicks, bestSoFar, decisionOf, seriesColor, wpText, spreadLabels } from '../ledger.js';
import { parseMachines, machinesText } from '../ui.js';
import { depsOf, depEntries } from '../task.js';
import { fold, Folder, LiveStrip } from '../run.js';
import { setLang } from '../i18n.js';

setLang('zh');  // these expectations are the Chinese texts; i18n.test.js covers the English

test('statusLine says what a task waits for or does', () => {
  const titles = { 'develop/a': 'A 任务' };
  const line = (status, detail, extra = {}) => statusLine({ status, detail, ...extra }, (id) => titles[id] || id);
  assert.equal(line('queued', { kind: 'upstream', tasks: ['develop/a', 'develop/b'] }), '等待上游任务：A 任务、develop/b');
  assert.equal(line('queued', {}), '上游已满足，等待调度');
  assert.equal(line('queued', { kind: 'archived' }), '项目已归档，任务不会自动开始');
  assert.equal(line('needs_you', { kind: 'unmanaged' }, { progress: { next: '等我决定' } }), '阻塞：等我决定');
  assert.equal(line('needs_you', { kind: 'budget' }, { cost_usd: 200, budget_usd: 200 }), '预算已用尽：$200.00 / $200.00');
  assert.equal(line('in_review', { kind: 'review' }, { report: 'pr-report.md' }), '报告：pr-report.md');
  assert.equal(line('done', {}, { progress: { next: '无' } }), '');
  assert.equal(line('unknown', { raw: '暂停' }), 'progress 状态：暂停');
});

const task = (id, status, started, extra = {}) => ({ id, type: id.split('/')[0], status, started, created: started, ...extra });

test('layout: lanes are columns, start days are rows, drafts go to the band at the bottom', () => {
  const tasks = [task('develop/a', 'done', '2026-10-20T01:00:00Z'), task('develop/b', 'running', '2026-10-21T01:00:00Z'),
    task('perf/c', 'queued', '2026-10-21T02:00:00Z'), task('develop/d', 'draft', '', { created: '2026-10-19T00:00:00Z' })];
  const edges = [{ from: 'develop/a', to: 'develop/b', kind: 'input' }, { from: 'develop/b', to: 'perf/c', kind: 'input' }];
  const L = layout(tasks, edges, ['develop', 'perf'], new Set(['develop/a']));
  const pos = (id) => L.pos.get(id);
  assert.equal(pos('develop/a').x, pos('develop/b').x);                 // same lane
  assert.ok(pos('perf/c').x > pos('develop/b').x);                        // next lane to the right
  assert.ok(pos('develop/a').y < pos('develop/b').y);                     // earlier day above
  assert.ok(pos('develop/b').y < pos('perf/c').y);                        // linked tasks never share a row
  assert.equal(pos('develop/a').h, 30);                                   // collapsed
  assert.ok(L.draftTop !== null && pos('develop/d').y > pos('perf/c').y);  // the draft is below every started task
  assert.deepEqual(L.rows.map((r) => r.draft), [false, false, false, true]);
});

test('layout: a template\'s first round runs from the top left to the bottom right, whatever the times say', () => {
  // made at once (one creation time); intake started a few seconds later, the rest wait for it
  const made = '2026-10-26T05:52:34Z';
  const tasks = ['plan/r1', 'profile/r1', 'baseline/r1', 'survey/r1'].map((id) => task(id, 'queued', '', { created: made }))
    .concat(task('intake/r1', 'needs_you', '2026-10-26T05:52:37Z', { created: made }));
  const edges = [['intake/r1', 'survey/r1'], ['intake/r1', 'baseline/r1'], ['baseline/r1', 'profile/r1'], ['profile/r1', 'plan/r1'],
    ['survey/r1', 'plan/r1'], ['intake/r1', 'plan/r1']].map(([from, to]) => ({ from, to, kind: 'input' }));
  const lanes = ['intake', 'survey', 'baseline', 'profile', 'plan'];
  const L = layout(tasks, edges, lanes, new Set());
  const row = (id) => L.pos.get(id).layer;
  assert.deepEqual(lanes.map((l) => row(`${l}/r1`)), [0, 1, 1, 2, 3]);
  assert.ok(lanes.every((l, i) => !i || L.pos.get(`${l}/r1`).x > L.pos.get(`${lanes[i - 1]}/r1`).x));
  // a cycle does not hang the layout
  const loop = layout(tasks, [...edges, { from: 'plan/r1', to: 'intake/r1', kind: 'input' }], lanes, new Set());
  assert.equal(loop.pos.size, 5);
  // a task that started before its upstream (re-run upstream) keeps its own time: the edge points up
  const rerun = [task('a/x', 'done', '2026-10-21T01:00:00Z'), task('b/y', 'done', '2026-10-20T01:00:00Z')];
  const R = layout(rerun, [{ from: 'a/x', to: 'b/y', kind: 'input' }], ['a', 'b'], new Set());
  assert.ok(R.pos.get('b/y').layer < R.pos.get('a/x').layer);
});

test('edgePaths: one SVG path per edge, from the upstream card to the downstream card', () => {
  const tasks = [task('develop/a', 'done', '2026-10-20T01:00:00Z'), task('develop/b', 'running', '2026-10-21T01:00:00Z'),
    task('develop/c', 'queued', '2026-10-22T01:00:00Z')];
  const edges = [{ from: 'develop/a', to: 'develop/b', kind: 'input' }, { from: 'develop/a', to: 'develop/c', kind: 'code' }];
  const L = layout(tasks, edges, ['develop'], new Set());
  const [ab, ac] = edgePaths(edges, L.pos);
  const a = L.pos.get('develop/a'), b = L.pos.get('develop/b');
  assert.ok(ab.startsWith(`M${a.x + a.w / 2},${a.y + a.h} C`) && ab.endsWith(`${b.x + b.w / 2},${b.y}`));  // next row: bottom to top
  assert.ok(ac.startsWith(`M${a.x + a.w},`));                                                                  // same lane, further down: round the right side
});

test('laneAt and moveLane: where a dragged lane title or card lands, and the lane order after a lane moves', () => {
  const L = layout([task('a/x', 'done', '2026-10-20T01:00:00Z'), task('b/y', 'done', '2026-10-20T02:00:00Z')], [], ['a', 'b', 'c'], new Set());
  const x0 = L.pos.get('a/x').x, x1 = L.pos.get('b/y').x;
  assert.equal(laneAt(x0 + 124, 3), 0);                    // the middle of a card in the first lane
  assert.equal(laneAt(x1 + 124, 3), 1);
  assert.equal(laneAt(x1 + 124 + 2 * (x1 - x0), 3), 2);    // past the last lane: the last one
  assert.equal(laneAt(-500, 3), 0);
  assert.deepEqual(moveLane(['a', 'b', 'c', 'd'], ['a', 'b', 'c', 'd'], 0, 2), ['b', 'c', 'a', 'd']);
  assert.deepEqual(moveLane(['a', 'b', 'c', 'd'], ['a', 'b', 'c', 'd'], 3, 0), ['d', 'a', 'b', 'c']);
  // hidden lanes (b) stay where they are; the shown ones change places around them
  assert.deepEqual(moveLane(['a', 'b', 'c', 'd'], ['a', 'c', 'd'], 0, 2), ['c', 'b', 'd', 'a']);
});

test('depsOf and depEntries: depends_on as the form edits it, and back as written', () => {
  const fm = { depends_on: ['develop/a', { task: 'develop/b', kind: 'code', label: 'L' }, { kind: 'input' }, { task: 'develop/c' }] };
  const deps = depsOf(fm);
  assert.deepEqual(deps, [{ task: 'develop/a', kind: 'input', label: '' }, { task: 'develop/b', kind: 'code', label: 'L' },
    { task: 'develop/c', kind: 'input', label: '' }]);
  assert.deepEqual(depEntries(deps), ['develop/a', { task: 'develop/b', kind: 'code', label: 'L' }, 'develop/c']);
  assert.deepEqual(depsOf({ depends_on: 'develop/a' }), [{ task: 'develop/a', kind: 'input', label: '' }]);
  assert.deepEqual(depsOf({}), []);
});

test('fold: tool calls become one line with their result; messages sent mid-command wait for it', () => {
  const ev = [
    { type: 'system', subtype: 'init', model: 'claude-haiku-4-5', ts: 1 },
    { type: 'runner', subtype: 'send_user', text: '开始', ts: 1 },
    { type: 'system', subtype: 'session_state_changed', state: 'running', ts: 2 },
    { type: 'assistant', message: { content: [{ type: 'text', text: '先跑一下' }, { type: 'tool_use', id: 't1', name: 'Bash', input: { command: 'sleep 40' } }] }, ts: 3 },
    { type: 'runner', subtype: 'send_user', text: '只看 C96', ts: 4 },
  ];
  let { items, inFlight } = fold(ev);
  assert.deepEqual(items.map((i) => i.kind), ['note', 'user', 'text', 'tool', 'user']);
  assert.equal(inFlight.length, 1);
  assert.equal(items[4].delivery, '已送达，将在当前命令结束后读取');
  ({ items, inFlight } = fold([...ev, { type: 'user', message: { content: [{ type: 'tool_result', tool_use_id: 't1', content: 'ok' }] }, ts: 44 }]));
  assert.equal(inFlight.length, 0);
  assert.equal(items[3].output, 'ok');
  assert.equal(items[4].delivery, '已读取');
});

// Recorded runs (agent_sys_v2/tests/fixtures/events), as the server hands them out.
const recording = (name) => readFileSync(new URL(`../../../../tests/fixtures/events/${name}.jsonl`, import.meta.url), 'utf8')
  .trim().split('\n').map((line, seq) => ({ seq, ...JSON.parse(line) }));

test('fold p2_queue: a message sent mid-command is read when the command ends, in the same round', () => {
  const ev = recording('p2_queue');
  const sent = ev.findIndex((e) => e.subtype === 'send_user' && /NEW INSTRUCTION/.test(e.text));
  let { items, inFlight } = fold(ev.slice(0, sent + 1));
  assert.equal(inFlight.length, 1);                                    // sleep 20; echo step1 is running
  assert.equal(items.at(-1).delivery, '已送达，将在当前命令结束后读取');
  ({ items, inFlight } = fold(ev));
  const msg = items.find((i) => i.kind === 'user' && !i.first);
  assert.equal(msg.delivery, '已读取');
  assert.equal(inFlight.length, 0);
  assert.equal(items.filter((i) => i.kind === 'tool' && i.done).length, 2);
  assert.match(items.at(-1).text, /runner 已退出/);
  assert.equal(items.filter((i) => i.round).length, 1);                // one result: one round
});

test('fold p2_textgen: a message sent while the agent writes is read in the next round', () => {
  const ev = recording('p2_textgen');
  const { items, session } = fold(ev);
  const msg = items.find((i) => i.kind === 'user' && !i.first);
  assert.equal(msg.delivery, '已读取');
  const sent = ev.findIndex((e) => e.subtype === 'send_user' && /NEW MESSAGE/.test(e.text));
  assert.equal(fold(ev.slice(0, sent + 1)).items.at(-1).delivery, '已送达，将在本轮结束后读取');
  assert.equal(items.filter((i) => i.round).length, 2);                // the message made a second round
  assert.equal(session, 'idle');
});

test('fold p2_interrupt: 停止 kills the command; the round ends as stopped, not as a failure', () => {
  const { items, inFlight } = fold(recording('p2_interrupt'));
  assert.equal(inFlight.length, 0);
  assert.ok(items.some((i) => i.kind === 'note' && i.text === '已发送停止指令（interrupt）'));
  assert.ok(items.some((i) => i.text === '已中断：正在执行的命令已终止'));
  assert.ok(items.some((i) => i.text === '本轮已停止（手动停止，并非运行失败）'));
  assert.ok(items.find((i) => i.kind === 'tool').error);
});

test('fold p4_k9: a killed runner leaves the foreground command in flight and the background one listed', () => {
  const { items, inFlight, bg, session } = fold(recording('p4_k9'));
  assert.equal(inFlight.length, 1);
  assert.match(inFlight[0].summary, /sleep 60/);
  assert.equal(bg.length, 1);
  assert.equal(session, 'running');
  assert.ok(!items.some((i) => i.kind === 'note' && /runner (已|出错)/.test(i.text)));  // no exit record
});

test('fold p5_main: waiting in the background (idle, a background command), then woken when it ends', () => {
  const ev = recording('p5_main');
  const waitEnd = ev.findIndex((e) => e.subtype === 'task_updated');
  let { bg, session, items } = fold(ev.slice(0, waitEnd));
  assert.equal(session, 'idle');
  assert.equal(bg.length, 1);
  assert.ok(items.find((i) => i.kind === 'tool' && i.background));
  ({ bg, session, items } = fold(ev));
  assert.equal(bg.length, 0);
  assert.equal(items.filter((i) => /后台命令已结束/.test(i.text || '')).length, 1);  // task_updated + task_notification: one note
  assert.ok(items.some((i) => i.text === '本轮结束（由后台命令结束唤醒） · 会话累计 $0.04'));
});

test('Folder: fed one event at a time gives what fold gives', () => {
  const ev = recording('p5_msg');
  const f = new Folder();
  for (const e of ev) f.add(e);
  assert.deepEqual(f.view(), fold(ev));
  assert.equal(f.count, ev.length);
  assert.equal(f.lastSeq, ev.at(-1).seq);
});

test('LiveStrip: running, waiting and ended runs render (htm throws on markup it cannot parse)', () => {
  const run = { live: true, started: '2026-10-23T01:00:00Z' };
  const t = { since: '2026-10-23T01:00:05Z', progress: null };
  const cls = (v) => v.props.class;
  assert.equal(cls(LiveStrip({ run, inFlight: [], bg: [], session: 'running', t })), 'live-strip working');
  assert.equal(cls(LiveStrip({ run, inFlight: [{ name: 'Bash', summary: 'sleep 20', ts: 1 }], bg: [], session: 'running', t })), 'live-strip working');
  for (const progress of [null, { next: '等作业 42 结束' }]) {
    const v = LiveStrip({ run, inFlight: [], bg: [{ description: 'until done; do sleep 60; done' }], session: 'idle', t: { ...t, progress } });
    assert.equal(cls(v), 'live-strip waiting');
  }
  assert.equal(cls(LiveStrip({ run: { ...run, live: false, exit: 'interrupted', cost_usd: null }, inFlight: [], bg: [], session: 'idle', killed: true, t })), 'live-strip ended');
});

test('@ mentions: what is being typed, the tasks offered, the text after picking one', () => {
  assert.deepEqual(mentionAt('参考 @dev', 7), { start: 3, query: 'dev' });
  assert.deepEqual(mentionAt('@', 1), { start: 0, query: '' });
  assert.deepEqual(mentionAt('看(@develop/a', 12), { start: 2, query: 'develop/a' });
  assert.equal(mentionAt('me@host', 7), null);  // an e-mail address or git@host
  assert.equal(mentionAt('@dev x', 6), null);  // a space ends it
  assert.deepEqual(mentionAt('@dev x', 4), { start: 0, query: 'dev' });  // the caret decides
  const tasks = [{ id: 'develop/agent_sys_hang_fix', title: '修复卡顿' }, { id: 'bug_fix/hang', title: '另一个' },
    { id: 'develop/x', title: 'hang 相关' }];
  assert.deepEqual(mentionOptions(tasks, 'hang').map((x) => x.id), ['bug_fix/hang', 'develop/agent_sys_hang_fix', 'develop/x']);
  assert.deepEqual(mentionOptions(tasks, '卡顿').map((x) => x.id), ['develop/agent_sys_hang_fix']);
  assert.equal(mentionOptions(tasks, '', 2).length, 2);
  assert.deepEqual(insertMention('参考 @han 的结论', { start: 3, query: 'han' }, 'bug_fix/hang'),
    { text: '参考 @bug_fix/hang 的结论', caret: 17 });
  assert.deepEqual(insertMention('@', { start: 0, query: '' }, 'develop/x'), { text: '@develop/x ', caret: 11 });
});

test('board filters: dates a task was active in, and the upstream context they keep', () => {
  const now = Date.parse('2026-10-24T12:00:00Z'), day = 86400000;
  const t = (id, status, started, finished) => ({ id, type: 'develop', status, started, created: started, finished, since: finished || started });
  const old = t('develop/old', 'done', '2026-10-01T00:00:00Z', '2026-10-02T00:00:00Z');
  const mid = t('develop/mid', 'done', '2026-10-10T00:00:00Z', '2026-10-20T00:00:00Z');
  const open = t('develop/open', 'running', '2026-10-05T00:00:00Z', '');
  const loose = t('develop/loose', 'queued', '2026-10-23T00:00:00Z', '');
  // the last n days keep what was active since then: unfinished tasks always
  const d7 = dateRange({ window: 'd7' }, now);
  assert.equal(d7.from, now - 7 * day);
  assert.deepEqual([old, mid, open, loose].filter((x) => inDates(x, d7, now)).map((x) => x.id), ['develop/mid', 'develop/open', 'develop/loose']);
  assert.deepEqual(dateRange({ window: 'all' }, now), { from: null, to: null });
  // picked dates, both included: a task overlapping them is in
  const r = dateRange({ window: 'custom', from: '2026-10-03', to: '2026-10-09' }, now);
  assert.deepEqual([old, mid, open, loose].filter((x) => inDates(x, r, now)).map((x) => x.id), ['develop/open']);
  assert.ok(inDates(old, dateRange({ window: 'custom', from: '', to: '2026-10-01' }, now), now));
  assert.ok(!inDates(loose, dateRange({ window: 'custom', from: '', to: '2026-10-22' }, now), now));
  // a task in the dates keeps its upstream from before them on the board, collapsed
  const view = { hiddenTypes: new Set(), statuses: new Set(), kinds: new Set(['input']), window: 'custom', from: '2026-10-21', to: '' };
  const edges = [{ from: 'develop/mid', to: 'develop/loose', kind: 'input' }];
  const v = visibleTasks([old, mid, open, loose], edges, view, false, now);
  assert.deepEqual(v.shown.map((x) => x.id), ['develop/mid', 'develop/open', 'develop/loose']);
  assert.deepEqual([...v.compact], ['develop/mid']);
  assert.equal(v.hidden, 1);
});

test('ledger numbers: compact values, deltas, round ticks, the best so far, the decision of a result', () => {
  assert.equal(fmt(12345.6), '12.3K');
  assert.equal(fmt(9850), '9.85K');
  assert.equal(fmt(123456), '123K');
  assert.equal(fmt(2_500_000), '2.5M');
  assert.equal(fmt(31.234), '31.23');
  assert.equal(fmt(undefined), '—');
  assert.equal(deltaText(15.437), '+15.4%');
  assert.equal(deltaText(-3.2), '−3.20%');
  assert.equal(deltaText(0, { baseline: true }), '基线');
  assert.equal(deltaText(null), '—');
  assert.deepEqual(ticks(0, 100, 4), [0, 50, 100]);  // 1, 2 or 5 times a power of ten apart
  assert.deepEqual(ticks(11800, 14200, 4), [12000, 13000, 14000]);
  assert.deepEqual(ticks(0.5, 0.9, 4), [0.5, 0.6, 0.7, 0.8, 0.9]);
  // time ticks fall on round local hours or days
  const t0 = new Date(2026, 9, 20, 5, 40).getTime(), t1 = new Date(2026, 9, 22, 2, 30).getTime();
  const tt = timeTicks(t0, t1, 5);
  assert.ok(tt.ticks.every((t) => new Date(t).getMinutes() === 0) && tt.ticks.length <= 5 && tt.ticks.length >= 2);
  assert.ok(timeTicks(t0, t0 + 30 * 86400000, 5).days);
  const r = (time, value, extra = {}) => ({ time, value, status: 'valid', decision: '', ...extra });
  const rows = [r('2026-10-01T00:00:00Z', 100), r('2026-10-02T00:00:00Z', 120, { decision: 'reject' }), r('2026-10-03T00:00:00Z', 110),
    r('2026-10-04T00:00:00Z', 130, { status: 'void' }), r('2026-10-05T00:00:00Z', 105)];
  assert.deepEqual(bestSoFar(rows, 1).map((s) => s.v), [100, 110, 110]);
  assert.deepEqual(bestSoFar(rows, -1).map((s) => s.v), [100, 100, 100]);
  assert.equal(decisionOf({ status: 'void', decision: 'accept' }), 'void');
  assert.equal(decisionOf({ status: 'valid', baseline: true }), 'baseline');
  assert.equal(decisionOf({ status: 'valid', role: 'candidate', decision: 'noise' }), 'noise');
  assert.equal(decisionOf({ status: 'valid', role: 'candidate', decision: '' }), 'none');
});

test('machines as the settings form edits them', () => {
  const list = parseMachines('smc-a16-19 8 MI300X node\n\n  smc-a16-20  \nbox 4');
  assert.deepEqual(list, [{ name: 'smc-a16-19', gpus: 8, note: 'MI300X node' }, { name: 'smc-a16-20' }, { name: 'box', gpus: 4 }]);
  assert.equal(machinesText(list), 'smc-a16-19 8 MI300X node\nsmc-a16-20\nbox 4');
  assert.equal(machinesText(['a', { name: 'b', gpus: 2 }]), 'a\nb 2');
});

test('children: the wait reason, the edge kind, and how many subtasks are finished', () => {
  const titles = { 'plan/r1': '计划' };
  assert.equal(statusLine({ status: 'queued', detail: { kind: 'children', parents: ['plan/r1'], tasks: ['experiment/a', 'experiment/b'] } }, (id) => titles[id] || id),
    '等待 计划 的子任务：experiment/a、experiment/b');
  assert.ok(BLOCKING.has('children') && EDGE_KINDS.children.label === '等子任务');
  const tasks = [
    { id: 'experiment/a', parent: 'plan/r1', status: 'done' },
    { id: 'experiment/b', parent: 'plan/r1', status: 'cancelled' },
    { id: 'experiment/c', parent: 'plan/r1', status: 'draft' },
    { id: 'close/r1', parent: 'plan/r1', status: 'queued' },  // the waiting task itself does not count
    { id: 'other/x', parent: 'plan/r2', status: 'queued' },
  ];
  assert.deepEqual(childrenDone(tasks, 'plan/r1', 'close/r1'), { done: 2, total: 3 });
});

test('ledger curves: a series keeps its color, the working point in words, end labels kept apart', () => {
  assert.equal(seriesColor(0), 'var(--series-1)');
  assert.equal(seriesColor(7), 'var(--series-8)');
  assert.equal(seriesColor(8), 'var(--ink-3)');  // past the eight slots: gray, never a made-up hue
  assert.equal(wpText({ x: 30 }, 'p90_intvty'), 'p90_intvty ≥ 30');
  assert.equal(wpText({ conc: 64 }, 'p90_intvty'), 'C64');
  assert.equal(wpText({}, 'p90_intvty'), '');
  assert.deepEqual(spreadLabels([{ name: 'b', y: 105 }, { name: 'a', y: 100 }, { name: 'c', y: 200 }]).map((l) => [l.name, l.y]),
    [['a', 100], ['b', 114], ['c', 200]]);
  // a reference is never the best so far
  const rows = [
    { value: 100, time: '2026-10-01T00:00:00Z', status: 'valid', decision: '', role: 'baseline' },
    { value: 900, time: '2026-10-02T00:00:00Z', status: 'valid', decision: '', role: 'reference' },
    { value: 120, time: '2026-10-03T00:00:00Z', status: 'valid', decision: 'accept', role: 'candidate' },
  ];
  assert.deepEqual(bestSoFar(rows, 1).map((s) => s.v), [100, 120]);
});
