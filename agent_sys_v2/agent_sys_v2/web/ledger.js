// Ledger: the results a campaign measured (results/*.json of its tasks, schema infera.result/v1), against the
// baseline. The page leads with the numbers (primary metric, baseline, best and its delta), then the trajectory (one
// dot per result over time, its A/B decision as shape + color, the baseline dashed, the best so far as a step line),
// then the table, which holds every value the chart shows. The board's head shows the same numbers (LedgerChips).
import { useState, useEffect, useRef } from './vendor/hooks.module.js';
import { html, Icon, useApi, clock, taskHref, ErrorBox } from './ui.js';
import { t as tr } from './i18n.js';

// ---------------------------------------------------------------- numbers

// 9850 -> 9.85K, 12345.6 -> 12.3K, 31.234 -> 31.23: compact from a thousand (three significant digits), four below.
export function fmt(v) {
  if (typeof v !== 'number' || !Number.isFinite(v)) return '—';
  const a = Math.abs(v);
  if (a >= 1e6) return `${+(v / 1e6).toPrecision(3)}M`;
  if (a >= 1e3) return `${+(v / 1e3).toPrecision(3)}K`;
  return `${+v.toPrecision(4)}`;
}

// A delta against the baseline: "+15.4%" (better or worse is the caller's color), "基线" for the baseline itself.
export function deltaText(d, { baseline = false } = {}) {
  if (baseline && (d == null || d === 0)) return tr('基线');
  if (typeof d !== 'number') return '—';
  return `${d > 0 ? '+' : d < 0 ? '−' : '±'}${Math.abs(d).toFixed(Math.abs(d) >= 10 ? 1 : 2)}%`;
}

// The A/B decision of a result: its word, its mark (shape + color class) and its legend order.
export const DECISIONS = {
  accept: { get label() { return tr('接受'); }, mark: 'dot', cls: 'd-accept' },
  reject: { get label() { return tr('拒绝'); }, mark: 'cross', cls: 'd-reject' },
  noise: { get label() { return tr('噪声'); }, mark: 'dot', cls: 'd-noise' },
  rerun: { get label() { return tr('需重跑'); }, mark: 'dot', cls: 'd-rerun' },
  void: { get label() { return tr('作废'); }, mark: 'ring', cls: 'd-void' },
  none: { get label() { return tr('未判定'); }, mark: 'dot', cls: 'd-none' },
  baseline: { get label() { return tr('基线'); }, mark: 'diamond', cls: 'd-baseline' },
};
export const decisionOf = (r) => (r.status === 'void' ? 'void' : r.baseline || r.role === 'baseline' ? 'baseline' : r.decision || 'none');

// ---------------------------------------------------------------- board chips

export function LedgerChips({ pid, ledger }) {
  const href = `#/p/${pid}/ledger`;
  if (!ledger?.count) return html`<a class="pill ledger-chip muted" href=${href} title=${tr('结果台账：任务写在 results/ 下的 infera.result 文件')}><${Icon} name="chart" />${tr('台账：暂无结果')}</a>`;
  const { baseline: b, best, primary } = ledger;
  const up = ledger.higher_is_better ? 1 : -1;
  return html`<a class="pill ledger-chip" href=${href} title=${tr('结果台账：主指标 {metric}', { metric: primary })}>
    <${Icon} name="chart" /><span class="muted">${primary}</span>
    ${b && html`<span>${tr('基线')} <b>${fmt(b.value)}</b></span>`}
    ${best && best !== b && best.delta_pct != null && html`<span>${tr('最佳')} <b>${fmt(best.value)}</b>
      <span class=${best.delta_pct * up > 0 ? 'ok-text' : best.delta_pct * up < 0 ? 'warn-text' : 'muted'}>${deltaText(best.delta_pct)}</span></span>`}</a>`;
}

// ---------------------------------------------------------------- the page

export function LedgerPage({ pid }) {
  const { data, error, reload } = useApi(`/api/projects/${pid}/ledger`, (c) => !c.project || c.project === pid);
  const [only, setOnly] = useState(null);  // a decision to show alone, or null for all
  if (error && !data) return html`<main id="main" class="page"><${ErrorBox} what=${tr('加载台账')} error=${error} retry=${reload} /></main>`;
  if (!data) return html`<main id="main" class="page"><div class="loading">${tr('正在加载台账…')}</div></main>`;
  const up = data.higher_is_better ? 1 : -1;
  const rows = data.rows.filter((r) => !only || decisionOf(r) === only);
  const extra = data.metrics.filter((k) => k !== data.primary).slice(0, 4);
  const present = Object.keys(DECISIONS).filter((k) => data.rows.some((r) => decisionOf(r) === k));
  return html`<main id="main" class="page ledger-page">
    ${error && html`<${ErrorBox} what=${tr('刷新台账')} error=${error} retry=${reload} />`}
    <header class="page-head"><div><h1>${tr('台账')}</h1>
      <p class="muted small">${tr('各任务写在工作区 results/ 下的测量结果（infera.result/v1）。Δ 是主指标相对基线的变化。')}</p></div></header>
    ${!data.count ? html`<${EmptyLedger} errors=${data.errors} pid=${pid} />` : html`
      <section class="stat-row" aria-label=${tr('概览')}>
        <div class="stat"><span class="muted small">${tr('主指标')}</span><b class="mono">${data.primary || '—'}</b>
          <span class="muted small">${data.higher_is_better ? tr('越大越好') : tr('越小越好')}</span></div>
        <div class="stat"><span class="muted small">${tr('基线')}</span><b>${fmt(data.baseline?.value)}</b>
          ${data.baseline ? html`<a class="small" href=${taskHref(pid, data.baseline.task, { tab: 'docs', file: data.baseline.file })}>${data.baseline.label}</a>`
            : html`<span class="muted small">${tr('还没有基线（role: baseline）')}</span>`}</div>
        <div class="stat"><span class="muted small">${tr('最佳')}</span><b>${fmt(data.best?.value)}
          ${data.best?.delta_pct != null && html` <span class=${data.best.delta_pct * up > 0 ? 'ok-text' : 'muted'}>${deltaText(data.best.delta_pct)}</span>`}</b>
          ${data.best && html`<a class="small" href=${taskHref(pid, data.best.task, { tab: 'docs', file: data.best.file })}>${data.best.label}</a>`}</div>
        <div class="stat"><span class="muted small">${tr('结果')}</span><b>${data.count}</b>
          <span class="muted small">${tr('作废 {n}', { n: data.void })}${data.errors.length ? tr('，格式错误 {n}', { n: data.errors.length }) : ''}</span></div>
      </section>
      <section class="chart-card" aria-label=${tr('趋势')}>
        <div class="legend-row" role="group" aria-label=${tr('按判定筛选')}>
          ${present.map((k) => html`<button class="chip" aria-pressed=${only === k} onClick=${() => setOnly(only === k ? null : k)}>
            <svg width="14" height="14" aria-hidden="true"><${Mark} kind=${k} x=${7} y=${7} /></svg>${DECISIONS[k].label}</button>`)}
          <span class="legend-line"><svg width="22" height="10" aria-hidden="true"><line class="base-line" x1="1" y1="5" x2="21" y2="5" /></svg>${tr('基线')}</span>
          <span class="legend-line"><svg width="22" height="10" aria-hidden="true"><line class="best-line" x1="1" y1="5" x2="21" y2="5" /></svg>${tr('目前最佳')}</span>
        </div>
        <${Trajectory} data=${data} rows=${rows} pid=${pid} />
      </section>
      <${LedgerTable} pid=${pid} rows=${[...rows].reverse()} data=${data} extra=${extra} up=${up} />
      ${data.errors.length > 0 && html`<${BadFiles} errors=${data.errors} pid=${pid} />`}`}
  </main>`;
}

function EmptyLedger({ errors, pid }) {
  return html`<section class="empty-state">
    <p><b>${tr('还没有结果。')}</b>${tr('任务把每个测量点写成工作区里的 results/<名字>.json，台账就会收进来。最少要有：')}</p>
    <pre>${'{"schema": "infera.result/v1", "label": "c64-baseline", "role": "baseline",\n "metrics": {"tok_s_gpu": 12345.6, "p90_interactivity": 30.1}}'}</pre>
    <p class="muted small">${tr('skill infera-bench 会按这个格式写；A/B 的判定写在 verdict（infera-experiment 的 ab_judge.py）。')}</p>
    ${errors.length > 0 && html`<${BadFiles} errors=${errors} pid=${pid} />`}
  </section>`;
}

function BadFiles({ errors, pid }) {
  return html`<section class="bad-files"><h2><${Icon} name="warning" />${tr('格式错误的结果文件 {n}', { n: errors.length })}</h2>
    <ul>${errors.map((e) => html`<li><a class="mono small" href=${taskHref(pid, e.task, { tab: 'docs', file: e.file })}>${e.task}/${e.file}</a>
      <span class="warn-text small">${e.errors.join('; ')}</span></li>`)}</ul></section>`;
}

function LedgerTable({ pid, rows, data, extra, up }) {
  return html`<section class="table-wrap"><h2>${tr('结果')} <span class="muted small">${tr('新的在前')}</span></h2>
    <table class="tasks ledger">
      <thead><tr><th>${tr('时间')}</th><th>${tr('任务 / 标签')}</th><th>${tr('改动')}</th><th class="num">${data.primary}</th><th class="num">Δ</th>
        ${extra.map((k) => html`<th class="num mono">${k}</th>`)}<th>${tr('判定')}</th><th>${tr('说明')}</th></tr></thead>
      <tbody>${rows.map((r) => {
        const k = decisionOf(r);
        return html`<tr class=${r.status === 'void' ? 'void' : ''}>
          <td class="nowrap small">${clock(r.time)}</td>
          <td><a href=${taskHref(pid, r.task, { tab: 'docs', file: r.file })}><b>${r.label}</b></a><div class="small muted">${r.title}</div></td>
          <td class="small">${r.change}</td>
          <td class="num"><b>${fmt(r.value)}</b></td>
          <td class=${`num ${r.delta_pct == null || r.status === 'void' || r.decision === 'noise' ? 'muted' : r.delta_pct * up > 0 ? 'ok-text' : r.delta_pct * up < 0 ? 'warn-text' : ''}`}>${deltaText(r.delta_pct, r)}</td>
          ${extra.map((m) => html`<td class="num">${fmt(r.metrics[m])}</td>`)}
          <td class="nowrap"><svg width="14" height="14" aria-hidden="true"><${Mark} kind=${k} x=${7} y=${7} /></svg> ${DECISIONS[k].label}</td>
          <td class="small">${r.notes}</td></tr>`;
      })}</tbody></table></section>`;
}

// ---------------------------------------------------------------- the chart

const H = 280, M = { l: 56, r: 132, t: 16, b: 30 };
const HOUR = 3600e3, DAY = 24 * HOUR;

// The chart is drawn at the width it is shown at, so its text stays the size of the page's.
function useWidth(fallback = 960) {
  const ref = useRef();
  const [w, setW] = useState(fallback);
  useEffect(() => {
    if (!ref.current || typeof ResizeObserver === 'undefined') return undefined;
    const ro = new ResizeObserver(([e]) => setW(Math.max(480, Math.round(e.contentRect.width))));
    ro.observe(ref.current);
    return () => ro.disconnect();
  }, []);
  return [ref, w];
}

// Time ticks on round hours or days (local time), about n of them: [ms].
export function timeTicks(t0, t1, n = 5) {
  const step = [HOUR, 3 * HOUR, 6 * HOUR, 12 * HOUR, DAY, 2 * DAY, 7 * DAY, 30 * DAY].find((s) => (t1 - t0) / s <= n) || 30 * DAY;
  const first = new Date(t0);
  first.setHours(0, 0, 0, 0);
  const out = [];
  for (let t = first.getTime(); t <= t1; t += step) if (t >= t0) out.push(t);
  return { ticks: out, days: step >= DAY };
}

// A result's mark: a dot for a decision, a cross for a rejection, a ring for a voided result, a diamond for the baseline.
function Mark({ kind, x, y, r = 4.5 }) {
  const d = DECISIONS[kind] || DECISIONS.none;
  if (d.mark === 'cross') return html`<path class=${`mark ${d.cls}`} d=${`M${x - r},${y - r}L${x + r},${y + r}M${x + r},${y - r}L${x - r},${y + r}`} />`;
  if (d.mark === 'diamond') return html`<path class=${`mark ${d.cls}`} d=${`M${x},${y - r - 1}L${x + r + 1},${y}L${x},${y + r + 1}L${x - r - 1},${y}Z`} />`;
  return html`<circle class=${`mark ${d.cls}`} cx=${x} cy=${y} r=${r} />`;
}

// Round tick values for [lo, hi]: 1, 2 or 5 times a power of ten apart, about n of them.
export function ticks(lo, hi, n = 4) {
  if (!(hi > lo)) return [lo];
  const raw = (hi - lo) / n, p = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 5, 10].map((m) => m * p).find((s) => s >= raw);
  const out = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi + step * 1e-9; v += step) out.push(+v.toPrecision(12));
  return out;
}

// The best value so far at each result, in time order: the step line's corners. Only valid results that were not
// rejected count, as for the ledger's best.
export function bestSoFar(rows, up) {
  let best = null;
  const out = [];
  for (const r of rows) {
    if (typeof r.value !== 'number' || r.status !== 'valid' || r.decision === 'reject') continue;
    if (best == null || r.value * up > best * up) best = r.value;
    out.push({ t: Date.parse(r.time), v: best });
  }
  return out;
}

function Trajectory({ data, rows, pid }) {
  const [tip, setTip] = useState(null);
  const [box, W] = useWidth();
  const pts = rows.filter((r) => typeof r.value === 'number' && Number.isFinite(Date.parse(r.time)));
  if (!pts.length) return html`<p class="empty-line">${tr('没有可画的结果（缺少主指标 {metric}）。', { metric: data.primary })}</p>`;
  const up = data.higher_is_better ? 1 : -1;
  const times = pts.map((r) => Date.parse(r.time));
  let t0 = Math.min(...times), t1 = Math.max(...times);
  if (t1 - t0 < 3600e3) { t0 -= 1800e3; t1 += 1800e3; }
  const base = data.baseline?.value;
  const vals = [...pts.map((r) => r.value), ...(typeof base === 'number' ? [base] : [])];
  let lo = Math.min(...vals), hi = Math.max(...vals);
  const pad = (hi - lo) * 0.12 || Math.abs(hi) * 0.05 || 1;
  lo -= pad; hi += pad;
  const ys = ticks(lo, hi);
  const x = (t) => M.l + ((t - t0) / (t1 - t0)) * (W - M.l - M.r);
  const y = (v) => H - M.b - ((v - lo) / (hi - lo)) * (H - M.t - M.b);
  const steps = bestSoFar(pts, up);
  const path = steps.map((s, i) => `${i ? `H${x(s.t)}V${y(s.v)}` : `M${x(s.t)},${y(s.v)}`}`).join('') + (steps.length ? `H${W - M.r}` : '');
  const last = steps[steps.length - 1];
  const xt = timeTicks(t0, t1, Math.max(3, Math.floor(W / 160)));
  const show = (r, ev) => setTip({ r, x: x(Date.parse(r.time)), y: y(r.value) });
  return html`<div class="chart" ref=${box} onMouseLeave=${() => setTip(null)}>
    <svg viewBox=${`0 0 ${W} ${H}`} width=${W} height=${H} role="img" aria-label=${tr('{metric} 随时间的变化：{n} 个结果', { metric: data.primary, n: pts.length })}>
      ${ys.map((v) => html`<line class="grid" x1=${M.l} x2=${W - M.r} y1=${y(v)} y2=${y(v)} />
        <text class="tick" x=${M.l - 8} y=${y(v) + 4} text-anchor="end">${fmt(v)}</text>`)}
      ${xt.ticks.map((v) => html`<text class="tick" x=${x(v)} y=${H - 8} text-anchor="middle">${clock(new Date(v).toISOString()).slice(0, xt.days ? 5 : 11)}</text>`)}
      ${typeof base === 'number' && html`<line class="base-line" x1=${M.l} x2=${W - M.r} y1=${y(base)} y2=${y(base)} />
        <text class="end-label" x=${W - M.r + 8} y=${y(base) + 4}>${tr('基线')} ${fmt(base)}</text>`}
      ${steps.length > 0 && html`<path class="best-line" d=${path} />
        ${(typeof base !== 'number' || Math.abs(y(last.v) - y(base)) > 14) && html`<text class="end-label" x=${W - M.r + 8} y=${y(last.v) + 4}>${tr('最佳')} ${fmt(last.v)}</text>`}`}
      ${pts.map((r) => html`<g key=${`${r.task}/${r.file}`}><${Mark} kind=${decisionOf(r)} x=${x(Date.parse(r.time))} y=${y(r.value)} />
        <a href=${taskHref(pid, r.task, { tab: 'docs', file: r.file })} aria-label=${`${r.label} ${fmt(r.value)} ${deltaText(r.delta_pct, r)} ${DECISIONS[decisionOf(r)].label}`}
          onFocus=${(ev) => show(r, ev)} onBlur=${() => setTip(null)}>
          <circle class="hit" cx=${x(Date.parse(r.time))} cy=${y(r.value)} r="12" onMouseEnter=${(ev) => show(r, ev)} /></a></g>`)}
    </svg>
    ${tip && html`<div class="chart-tip" style=${{ left: `${(tip.x / W) * 100}%`, top: `${(tip.y / H) * 100}%` }}>
      <b>${fmt(tip.r.value)}</b> <span class=${tip.r.delta_pct == null ? 'muted' : tip.r.delta_pct * up > 0 ? 'ok-text' : 'warn-text'}>${deltaText(tip.r.delta_pct, tip.r)}</span>
      <div>${tip.r.label} · ${DECISIONS[decisionOf(tip.r)].label}</div>
      <div class="muted">${tip.r.title} · ${clock(tip.r.time)}</div>
      ${tip.r.change && html`<div class="muted">${tip.r.change}</div>`}</div>`}
  </div>`;
}
