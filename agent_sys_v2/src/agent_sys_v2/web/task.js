// Task detail drawer (header; run output / workspace files / task settings tabs) and the new-task drawer.
import { useState, useEffect, useRef } from './vendor/hooks.module.js';
import { api, paths } from './api.js';
import { html, Icon, StatusMark, EDGE_KINDS, BLOCKING, statusLine, useApi, useNow, money, clock, since, ago, bytes,
  Markdown, AutoText, MentionText, ErrorBox, Dialog, confirmDialog, toast, hrefWith, go, taskHref, ModelSelect,
  EffortSelect, UltracodeCheck } from './ui.js';
import { RunTab, AttachCommand, endTakeover, BudgetButton } from './run.js';
// `t` is a task throughout this file: the translator is tr.
import { t as tr } from './i18n.js';

// Default tab by status.
function defaultTab(t) {
  if (!t.managed && ['running', 'needs_you', 'done', 'in_review'].includes(t.status)) return 'docs';
  if (t.status === 'needs_you') return t.questions || !t.runs.length ? 'docs' : 'run';
  if (t.status === 'in_review' || t.status === 'done') return 'docs';
  if (t.status === 'running' || t.status === 'cancelled') return t.runs.length ? 'run' : 'docs';
  return 'task';
}

export function TaskDrawer({ pid, tid, tab, file }) {
  const path = paths.task(pid, tid);
  const { data: t, error, reload } = useApi(path, (c) => c.project === pid);
  const head = useRef();
  // focus moves to the task's title once it is loaded, and back to whatever opened it on close
  const opener = useRef(document.activeElement);
  useEffect(() => { if (t) head.current?.focus(); }, [tid, !!t]);
  useEffect(() => () => { if (opener.current?.isConnected) opener.current.focus(); }, []);
  // opening a finished task counts as viewing its result
  useEffect(() => {
    if (t?.unread && t.status === 'done') api.post(`${path}/actions/mark_read`).catch(() => {});
  }, [t?.id, t?.unread]);
  const close = () => go(hrefWith({ t: null, tab: null, file: null, run: null }));
  useEffect(() => {
    const k = (ev) => { if (ev.key === 'Escape' && !document.querySelector('dialog[open]')) close(); };
    addEventListener('keydown', k);
    return () => removeEventListener('keydown', k);
  }, []);
  if (error && !t) {
    return html`<aside class="drawer" aria-label=${tr('任务详情')}><div class="drawer-head"><h2 tabIndex="-1" ref=${head}>${tid}</h2>
      <button class="btn icon-only ghost" onClick=${close} aria-label=${tr('关闭')}><${Icon} name="x" /></button></div>
      <div class="drawer-body"><${ErrorBox} what=${tr('加载任务')} error=${error} retry=${reload} /></div></aside>`;
  }
  if (!t) return html`<aside class="drawer" aria-label=${tr('任务详情')}><div class="drawer-body loading">${tr('正在加载任务…')}</div></aside>`;
  const active = tab || defaultTab(t);
  const setTab = (k) => go(hrefWith({ tab: k, file: k === 'docs' ? file : null }));
  const tabs = [['run', tr('运行输出'), t.runs.length ? `${t.runs.length}` : ''], ['docs', tr('工作区文件'), t.questions ? tr('{n} 个问题', { n: t.questions.count }) : ''],
    ['task', tr('任务配置'), '']];
  return html`<aside class="drawer" aria-label=${tr('任务详情：{title}', { title: t.title })}>
    <${Header} pid=${pid} t=${t} head=${head} close=${close} />
    <div class="tabs" role="tablist" aria-label=${tr('任务详情')}>
      ${tabs.map(([k, label, badge]) => html`<button role="tab" id=${`tab-${k}`} aria-selected=${active === k} aria-controls=${`panel-${k}`}
        tabIndex=${active === k ? 0 : -1} onClick=${() => setTab(k)}
        onKeyDown=${(ev) => { const i = tabs.findIndex((x) => x[0] === k); if (ev.key === 'ArrowRight' || ev.key === 'ArrowLeft') { const n = tabs[(i + (ev.key === 'ArrowRight' ? 1 : 2)) % 3][0]; setTab(n); setTimeout(() => document.getElementById(`tab-${n}`)?.focus()); } }}>
        ${label}${badge && html`<span class="badge">${badge}</span>`}</button>`)}
    </div>
    <div class="drawer-body" role="tabpanel" id=${`panel-${active}`} aria-labelledby=${`tab-${active}`}>
      ${active === 'run' && html`<${RunTab} pid=${pid} t=${t} />`}
      ${active === 'docs' && html`<${DocsTab} pid=${pid} t=${t} file=${file} />`}
      ${active === 'task' && html`<${TaskTab} pid=${pid} t=${t} />`}
    </div>
  </aside>`;
}

// ---------------------------------------------------------------- header

// A line of the head cut to one line with …; 展开 shows the whole text when it does not fit.
function OneLine({ class: cls, children }) {
  const ref = useRef();
  const [open, setOpen] = useState(false);
  const [over, setOver] = useState(false);
  const measure = () => { const el = ref.current; if (el && !open) setOver(el.scrollWidth > el.clientWidth + 1); };
  useEffect(measure);  // the text may change on every render
  useEffect(() => {
    const ro = new ResizeObserver(measure);
    ro.observe(ref.current);
    return () => ro.disconnect();
  }, [open]);
  return html`<div class=${`one-line ${cls}${open ? ' open' : ''}`}><p ref=${ref}>${children}</p>
    ${(over || open) && html`<button class="btn ghost small" aria-expanded=${open} onClick=${() => setOpen(!open)}>${open ? tr('收起') : tr('展开')}</button>`}</div>`;
}

function Header({ pid, t, head, close }) {
  const now = useNow(1000);
  const [dialog, setDialog] = useState(null);
  const [busy, setBusy] = useState(false);
  const guard = (fn) => () => { if (busy) return; setBusy(true); Promise.resolve(fn()).catch(() => {}).finally(() => setBusy(false)); };
  const base = paths.task(pid, t.id);
  const act = (action, body, msg) => api.post(`${base}/actions/${action}`, body).then((r) => { toast(msg); return r; }, (e) => { toast(e.message, { error: true }); throw e; });
  const line = statusLine(t, (id) => t.upstream.find((u) => u.task === id)?.title || id);
  const live = t.runs[0]?.live && t.runs[0].kind !== 'terminal';
  const approve = ['primary', tr('审核通过'), () => act('approve', {}, tr('已审核通过，下游任务可以开始'))];
  const takeover = ['terminal', tr('在终端中接管'), () => setDialog('takeover')];
  const b = [];  // [style, label, onClick]
  if (!t.managed) {
    if (t.status === 'in_review') b.push(approve);
    b.push([t.status === 'in_review' ? '' : 'primary', tr('纳入托管'), () => setDialog('join')]);
  } else if (t.status === 'queued') b.push(['primary', tr('开始'), () => act('start', {}, tr('任务已开始'))]);
  else if (t.status === 'running' && t.detail?.host) { /* runs on another host: this agent_sys only shows it */ }
  else if (t.status === 'running' && t.detail?.kind === 'terminal') b.push(['', tr('结束接管'), () => endTakeover(pid, t)]);
  else if (t.status === 'running') { b.push(['danger-ghost', tr('停止'), () => stop(base)]); b.push(takeover); }
  else if (t.status === 'needs_you') {
    if (/interrupted/.test(t.detail?.kind)) b.push(['primary', tr('继续'), () => act('resume', { text: '继续' }, tr('任务已继续运行'))]);  // the text goes to the agent  // i18n: to the agent
    if (t.detail?.kind === 'budget') b.push(['budget']);
    b.push(takeover);
  } else if (t.status === 'in_review') { b.push(approve); b.push(takeover); }
  else if (t.status === 'done') b.push(takeover);
  const more = [
    t.managed && !['cancelled', 'done'].includes(t.status) && [tr('取消任务'), () => setDialog('cancel')],
    [tr('删除任务'), () => setDialog('delete')],
  ].filter(Boolean);
  const drafts = t.downstream.filter((d) => d.kind === 'subtask' && d.status === 'draft').map((d) => ({ id: d.task, title: d.title }));
  const c = t.progress?.checks || {};
  const checks = { done: c.done || 0, total: (c.done || 0) + (c.doing || 0) + (c.todo || 0) };
  const elapsed = t.since && t.status !== 'draft'
    ? t.status === 'running' && t.detail?.kind !== 'waiting' ? tr('已运行 {time}', { time: since(t.since, now) }) : ['done', 'cancelled'].includes(t.status) ? ago(t.since, now) : tr('已等待 {time}', { time: since(t.since, now) }) : '';
  return html`<div class="drawer-head">
    <div class="head-top">
      <${StatusMark} t=${t} />
      ${t.unread && html`<span class="tag">${tr('未查看')}</span>`}
      ${!t.managed && html`<span class="tag" title=${tr('工作区中没有 .agent_sys/state.json：agent_sys 仅显示，不调度')}>${tr('未托管')}</span>`}
      ${t.review && html`<span class="tag" title=${tr('完成后先进入待审核，审核通过后才放行下游')}>${tr('完成后需审核')}</span>`}
      ${(t.machines || []).map((m) => html`<span class="tag mono" title=${tr('本任务使用的机器（运行中和待确认时占用）')}><${Icon} name="machine" />${m}</span>`)}
      <span class="spacer"></span>
      <a class="btn ghost small" href=${`#/p/${pid}?t=${pid}/${t.id}`}>${tr('在看板中定位')}</a>
      <button class="btn icon-only ghost" onClick=${close} aria-label=${tr('关闭详情（Esc）')}><${Icon} name="x" /></button>
    </div>
    <h2 tabIndex="-1" ref=${head}>${t.title}</h2>
    <div class="mono small muted">${pid}/${t.id}${t.parent ? html` · ${tr('父任务')} <a href=${taskHref(pid, t.parent)}>${t.parent}</a>` : ''}</div>
    ${line && html`<${OneLine} class=${`why st-${t.status}`}>${line}${elapsed && html`<span class="muted">${tr('（{elapsed}）', { elapsed })}</span>`}</${OneLine}>`}
    ${t.summary && html`<${OneLine} class="summary">${t.summary}</${OneLine}>`}
    <div class="head-meta">
      ${t.managed && html`<span class="cost" title=${tr('累计花费 / 预算')}><span class="meter" aria-hidden="true"><i style=${{ width: `${Math.min(100, (100 * t.cost_usd) / (t.budget_usd || 1))}%` }}></i></span>
        ${money(t.cost_usd)} / ${money(t.budget_usd)}</span>`}
      ${t.agent && t.managed && html`<span class="muted small">${t.agent.model} · ${t.agent.effort}${t.agent.ultracode ? ' · ultracode' : ''}</span>`}
      ${checks.total > 0 && html`<span class="muted small" title=${tr('progress.md 中已勾选的步骤 / 全部步骤')}>${tr('进度 {done}/{total}', checks)}</span>`}
      ${t.session_id && html`<span class="muted small mono" title=${tr('会话 ID')}>${tr('会话 {id}', { id: t.session_id.slice(0, 8) })}</span>`}
      ${t.checks && html`<a class=${`small ${t.checks.ok ? 'ok-text' : 'warn-text'}`} href=${taskHref(pid, t.id, { tab: 'docs', file: CHECKS })}
        title=${tr('最近一轮的验收检查（第 {n} 次）', { n: t.checks.attempts })}>${t.checks.ok ? '✓' : '✗'} ${tr('验收 {passed}/{total}', t.checks)}</a>`}
    </div>
    ${drafts.length > 0 && html`<div class="notice drafts"><${Icon} name="draft" /><div>${tr('这个任务起草了 {n} 个子任务：{titles}', { n: drafts.length, titles: drafts.map((d) => d.title).join(tr('、')) })}
      <button class="btn small primary" disabled=${busy} onClick=${guard(() => act('join_children', {}, tr('已纳入托管 {n} 个子任务', { n: drafts.length })))}>${tr('全部纳入托管')}</button></div></div>`}
    <div class="head-actions">
      ${b.map(([style, label, fn]) => style === 'budget' ? html`<${BudgetButton} pid=${pid} t=${t} />`
        : style === 'terminal' ? html`<button class="btn" onClick=${guard(fn)} disabled=${busy}><${Icon} name="terminal" />${label}</button>`
          : html`<button class=${`btn ${style}`} onClick=${guard(fn)} disabled=${busy}>${label}</button>`)}
      <${More} items=${more} />
    </div>
    ${dialog === 'takeover' && html`<${TakeoverDialog} pid=${pid} t=${t} live=${live} onClose=${() => setDialog(null)} />`}
    ${dialog === 'join' && html`<${JoinDialog} pid=${pid} t=${t} onClose=${() => setDialog(null)} />`}
    ${dialog === 'cancel' && html`<${SimpleConfirm} title=${tr('确认取消该任务？')} ok=${tr('取消任务')} danger onClose=${() => setDialog(null)}
      onOk=${() => act('cancel', {}, tr('任务已取消'))}><p>${tr('正在运行的 runner 将被停止。依赖该任务的下游任务会停留在"待开始"状态。')}</p></${SimpleConfirm}>`}
    ${dialog === 'delete' && html`<${SimpleConfirm} title=${tr('确认删除该任务？')} ok=${tr('删除任务')} danger onClose=${() => setDialog(null)}
      onOk=${() => act('delete', {}, tr('任务已删除，任务文件和工作区已移至 .trash/')).then(() => go(hrefWith({ t: null, tab: null, file: null })))}>
      <p>${tr('任务文件和工作区将移至项目的')} <code>.trash/</code> ${tr('目录，移回原处即可恢复。其他任务中对它的引用不会修改，之后显示为记录提醒。')}</p></${SimpleConfirm}>`}
  </div>`;
}

async function stop(base) {
  if (!await confirmDialog({ title: tr('确认停止本次运行？'), ok: tr('停止'), danger: true,
    body: html`<p>${tr('正在执行的命令将立即终止，任务转为"待确认"。会话保留，回复后可继续运行。')}</p>` })) return;
  return api.post(`${base}/actions/stop`).then(() => toast(tr('已停止，任务转为待确认')), (e) => toast(e.message, { error: true }));
}

function More({ items }) {
  const [open, setOpen] = useState(false);
  if (!items.length) return null;
  return html`<div class="menu-wrap"><button class="btn ghost" aria-haspopup="menu" aria-expanded=${open} onClick=${() => setOpen(!open)}>${tr('更多')}<${Icon} name="chevron" className="rot90" /></button>
    ${open && html`<div class="menu" role="menu" onKeyDown=${(e) => e.key === 'Escape' && setOpen(false)}>
      ${items.map(([label, fn]) => html`<button role="menuitem" onClick=${() => { setOpen(false); fn(); }}>${label}</button>`)}</div>`}</div>`;
}

function SimpleConfirm({ title, ok, danger, onClose, onOk, children }) {
  return html`<${Dialog} title=${title} onClose=${onClose}>${children}
    <div class="dialog-actions"><button class="btn ghost" onClick=${onClose}>${tr('返回')}</button>
      <button class=${`btn ${danger ? 'danger' : 'primary'}`} onClick=${() => { onClose(); onOk(); }}>${ok}</button></div></${Dialog}>`;
}

function TakeoverDialog({ pid, t, live, onClose }) {
  const [res, setRes] = useState(null);
  const start = () => api.post(`${paths.task(pid, t.id)}/actions/takeover`).then((r) => setRes(r.takeover), (e) => toast(e.message, { error: true }));
  const waiting = t.detail?.kind === 'waiting';
  return html`<${Dialog} title=${tr('在终端中接管')} onClose=${onClose}>
    ${!res ? html`
      <p>${live ? tr('agent_sys 将停止正在运行的 runner、清理该会话的残留进程，并在') : tr('agent_sys 将清理该会话的残留进程，并在')} <code>tmux -L agent_sys</code> ${tr('中以同一会话启动交互式 claude（权限和配置与 runner 一致）。')}</p>
      ${waiting && html`<p class="warn-text"><${Icon} name="warning" />${tr('该任务正在后台等待：接管会结束后台命令，退出后 agent 需要重新开始等待。')}</p>`}
      <ul class="small"><li>${tr('接管期间任务显示为"运行中 · 终端接管"，网页输入框暂停使用。')}</li>
        <li>${tr('在终端中退出 claude 后，agent_sys 将根据 progress.md 自动判断任务状态，无需手动标记。')}</li></ul>
      <div class="dialog-actions"><button class="btn ghost" onClick=${onClose}>${tr('取消')}</button><button class="btn primary" onClick=${start} autofocus>${tr('接管')}</button></div>`
    : html`<p>${tr('已在 tmux 窗口')} <code>${res.window}</code>${tr(' 中启动。请在终端中运行：')}</p>
      <${AttachCommand} attach=${res.attach} />
      <div class="dialog-actions"><button class="btn primary" onClick=${onClose} autofocus>${tr('确定')}</button></div>`}
  </${Dialog}>`;
}

export function JoinDialog({ pid, t, onClose }) {
  const done = t.progress && /^(已完成|完成|done)/i.test(t.progress.status || '');  // i18n: reads the agent's progress.md
  const body = t.status === 'draft' ? html`<p>${tr('纳入托管后任务进入"待开始"：上游任务全部完成且有运行名额时自动开始（需开启自动开始）；无上游的任务会立即开始。')}</p>`
    : done ? html`<p>${t.review ? tr('progress 显示已完成，纳入托管后直接标记为"待审核"（该任务设置了完成后需审核），不会重新运行。')
      : tr('progress 显示已完成，纳入托管后直接标记为"已完成"，不会重新运行。')}</p>`
      : html`<p class="warn-text"><${Icon} name="warning" />${tr('progress 显示为"{status}"：该任务可能仍在终端中运行。', { status: t.progress?.status ?? '' })}</p>
        <p>${tr('纳入托管后任务进入"待确认"，不会立即运行。请先确认终端中的会话已结束再回复；回复内容将与"工作区中已有进展，请先阅读 progress.md"一并作为新会话的首条消息。')}</p>`;
  return html`<${SimpleConfirm} title=${tr('确认将该任务纳入 agent_sys 托管？')} ok=${tr('纳入托管')} onClose=${onClose}
    onOk=${() => api.post(`${paths.task(pid, t.id)}/actions/join`).then(() => toast(tr('已纳入托管：agent_sys 已生成 .agent_sys/state.json 并开始管理该任务')), (e) => toast(e.message, { error: true }))}>
    ${body}<p class="muted small">${tr('纳入托管会在工作区生成')} <code>.agent_sys/state.json</code>${tr('（包含新的会话 ID）。重命名和修改类型仅对草稿开放，纳入托管后只能删除。')}</p></${SimpleConfirm}>`;
}

// ---------------------------------------------------------------- workspace files tab

function DocsTab({ pid, t, file }) {
  const base = paths.task(pid, t.id);
  const pinned = [
    t.questions && [t.questions.path, tr('{n} 个问题待回答', { n: t.questions.count })],
    t.report && [t.report, tr('报告')],
    ['progress.md', ''], ['issue.md', ''],
    t.check_runs && [CHECKS, t.checks ? `${t.checks.ok ? '✓' : '✗'} ${t.checks.passed}/${t.checks.total}` : '', tr('验收检查')],
  ].filter(Boolean);
  const current = file || (t.questions ? t.questions.path : t.report || (t.progress ? 'progress.md' : null));
  const open = (p) => go(hrefWith({ tab: 'docs', file: p }));
  return html`<div class="docs">
    <nav class="files" aria-label=${tr('工作区文件')}>
      <div class="files-title small muted">${tr('常用文件')}</div>
      ${pinned.map(([p, note, label]) => html`<button class=${`file ${p === current ? 'on' : ''}`} onClick=${() => open(p)}>
        <span class="ellipsis">${label || p}</span>${note && html`<span class="badge">${note}</span>`}</button>`)}
      ${t.handoff?.length > 0 && html`<${Handoff} items=${t.handoff} current=${current} open=${open} />`}
      <div class="files-title small muted" title=${t.workspace}>${tr('工作区')} <span class="mono">${t.workspace.split('/').slice(-2).join('/')}/</span></div>
      <${Dir} base=${base} dir="" current=${current} open=${open} depth=${0} scope=${(c) => c.task === t.id} />
    </nav>
    <div class="viewer">${current === CHECKS ? html`<${ChecksView} runs=${t.check_runs} checks=${t.checks} />`
      : current ? html`<${Viewer} key=${current} pid=${pid} t=${t} base=${base} path=${current} open=${open} />`
      : html`<div class="empty-state">${t.status === 'queued' ? tr('工作区为空（任务尚未开始）。') : tr('工作区为空。')}</div>`}</div>
  </div>`;
}

// The acceptance checks of the task's latest run: every attempt (a failed one went back to the agent), newest first,
// each check with its command, exit and the end of its output. Shown as a pinned "file" of the workspace.
export const CHECKS = ':checks';  // the pinned entry's id in the URL (file=:checks): not a path a workspace has
function ChecksView({ runs, checks }) {
  const attempts = [...(runs?.attempts || [])].reverse();
  if (!attempts.length) return html`<div class="empty-state">${tr('这一轮还没有运行验收检查。')}</div>`;
  return html`<div class="checks-view">
    <p class=${checks?.ok ? 'ok-text' : 'warn-text'}><b>${checks?.ok ? tr('最近一次验收通过') : tr('最近一次验收未通过')}</b>
      <span class="muted small"> ${tr('共 {n} 次（失败的那几次已把输出发回给 agent）', { n: attempts.length })}</span></p>
    ${attempts.map((a, i) => html`<details class="attempt" open=${i === 0}>
      <summary><span class=${a.ok ? 'ok-text' : 'warn-text'}>${a.ok ? '✓' : '✗'}</span> ${tr('第 {n} 次', { n: attempts.length - i })} · ${clock(a.time)}</summary>
      ${a.results.map((r) => html`<div class="check-row">
        <div><span class=${r.ok ? 'ok-text' : 'warn-text'}>${r.ok ? '✓' : '✗'}</span> <b>${r.name}</b>
          <span class="muted small">${r.timed_out ? tr('超时') : `exit ${r.exit}`} · ${r.seconds}s</span></div>
        ${r.name !== r.run && html`<code class="small">${r.run}</code>`}
        ${r.output && html`<pre>${r.output}</pre>`}</div>`)}</details>`)}
  </div>`;
}

// The report's handoff list: a file in the workspace opens here; a directory, a path outside the workspace and an
// item without a path (a branch, a PR) are shown as written; a path that is not there is marked.
function Handoff({ items, current, open }) {
  return html`<div class="files-title small muted" title=${tr('报告中的"## 交接清单"：下游任务的首条消息会带上它')}>${tr('交接清单')}</div>
    <ul class="handoff">${items.map((h) => html`<li title=${h.text}>${h.rel && h.kind === 'file'
      ? html`<button class=${`file ${h.rel === current ? 'on' : ''}`} onClick=${() => open(h.rel)}><span class="ellipsis">${h.text}</span></button>`
      : html`<span class=${`small ellipsis ${h.path && !h.exists ? 'warn-text' : 'muted'}`}>${h.path && !h.exists
        ? html`<${Icon} name="warning" label=${tr('路径不存在')} />` : ''}${h.text}</span>`}</li>`)}</ul>`;
}

function Dir({ base, dir, current, open, depth, scope }) {
  const { data, error } = useApi(`${base}/files?dir=${encodeURIComponent(dir)}`, scope);
  const [expanded, setExpanded] = useState({});
  if (error) return html`<div class="small error-text">${error.message}</div>`;
  if (!data) return html`<div class="small muted">…</div>`;
  if (!data.entries.length && depth === 0) return html`<div class="small muted">${tr('（空目录）')}</div>`;
  return html`<ul class="tree" role=${depth ? 'group' : 'tree'}>
    ${data.entries.map((e) => e.type === 'dir' ? html`<li role="treeitem" aria-expanded=${!!expanded[e.path]}>
        <button class="file dir" onClick=${() => setExpanded({ ...expanded, [e.path]: !expanded[e.path] })}>
          <${Icon} name="chevron" className=${expanded[e.path] ? 'rot90' : ''} /><span class="ellipsis">${e.name}/</span>
          <span class="muted small">${e.count}${e.count >= 1000 ? '+' : ''}</span></button>
        ${expanded[e.path] && html`<${Dir} base=${base} dir=${e.path} current=${current} open=${open} depth=${depth + 1}
          scope=${scope} />`}</li>`
      : html`<li role="treeitem"><button class=${`file ${e.path === current ? 'on' : ''}`} onClick=${() => open(e.path)} title=${`${e.path} · ${bytes(e.size)}`}>
          <span class="ellipsis">${e.name}</span><span class="muted small">${bytes(e.size)}</span></button></li>`)}
    ${data.truncated && html`<li class="small muted">${tr('共 {total} 项，仅显示前 {n} 项', { total: data.total, n: data.entries.length })}</li>`}
  </ul>`;
}

function Viewer({ pid, t, base, path, open }) {
  const { data: f, error, reload } = useApi(`${base}/file?path=${encodeURIComponent(path)}`, (c) => c.task === t.id);
  // while editing: {text, base}, base = the file's mtime when editing started. A refresh while you edit (the
  // agent wrote the file) must not move it, or saving would overwrite what the agent wrote.
  const [edit, setEdit] = useState(null);
  // this round's questions.md opens as a form to answer; 原文 shows the file, which can be edited
  const isQuestions = t.questions?.path === path;
  const [mode, setMode] = useState(isQuestions ? 'answer' : 'read');
  if (error) return html`<${ErrorBox} what=${tr('打开 {path}', { path })} error=${error} retry=${reload} />`;
  if (!f) return html`<div class="loading">${tr('正在加载 {path}…', { path })}</div>`;
  const dir = path.includes('/') ? path.slice(0, path.lastIndexOf('/')) : '';
  const save = () => api.put(`${base}/file?path=${encodeURIComponent(path)}`, { content: edit.text, base_mtime: edit.base })
    .then(() => { setEdit(null); toast(tr('已保存 {path}', { path })); reload(); }, (e) => toast(e.message, { error: true }));
  return html`<div class="viewer-inner">
    <div class="viewer-head"><span class="mono">${path}</span><span class="muted small">${bytes(f.size)} · ${clock(f.mtime)}</span>
      <span class="spacer"></span>
      ${isQuestions && html`<div class="seg" role="group" aria-label=${tr('显示方式')}>
        <button aria-pressed=${mode === 'answer'} onClick=${() => setMode('answer')}>${tr('表格回答')}</button>
        <button aria-pressed=${mode === 'read'} onClick=${() => setMode('read')}>${tr('原文')}</button></div>`}
      ${f.kind === 'md' && edit == null && mode === 'read' && html`<button class="btn small"
        onClick=${() => setEdit({ text: f.content, base: f.mtime })}>${tr('编辑')}</button>`}
      ${f.url && html`<a class="btn small ghost" href=${f.url} target="_blank" rel="noreferrer">${tr('在新窗口打开')}</a>`}</div>
    ${t.status === 'in_review' && path === t.report && edit == null && html`<${ReviewBar} pid=${pid} t=${t} />`}
    ${mode === 'answer' && isQuestions ? html`<${Questions} key=${t.questions.mtime} t=${t} base=${base} raw=${() => setMode('read')} />`
      : edit != null ? html`<div class="editor">
          ${f.mtime !== edit.base && html`<div class="notice"><${Icon} name="warning" /><div>${tr('{path} 在你编辑期间已被修改（{time}），保存会被拒绝。', { path, time: clock(f.mtime) })}
            <button class="btn small" onClick=${() => setEdit({ text: f.content, base: f.mtime })}>${tr('载入新版本')}</button>
            ${tr('（放弃你的修改）')}</div></div>`}
          <${AutoText} value=${edit.text} onInput=${(v) => setEdit({ ...edit, text: v })} onSubmit=${save} rows=${16} class="mono"
            aria-label=${tr('编辑 {path}', { path })} />
          <div class="row-actions"><button class="btn primary" onClick=${save}>${tr('保存')}</button>
            <button class="btn ghost" onClick=${() => setEdit(null)}>${tr('放弃修改')}</button>
            <span class="muted small">${tr('保存时会检查文件是否已被其他进程修改')}</span></div></div>`
        : f.unavailable ? html`<div class="empty-state">${f.unavailable}</div>`
          : f.kind === 'binary' ? html`<div class="empty-state">${tr('二进制文件（{size}），不显示内容。', { size: bytes(f.size) })}
            <a href=${api.rawUrl(pid, t.id, path)} download>${tr('下载')}</a></div>`
          : f.kind === 'image' ? html`<img class="doc-img" src=${f.url} alt=${path} />`
            : f.kind === 'md' ? html`<${Markdown} text=${f.content} base=${{ project: pid, task: t.id, dir }} onLink=${open} className="doc" />`
              : html`${f.truncated && html`<div class="muted small">${tr('仅显示最后 {n} 行（文件大小 {size}）', { n: f.tail_lines, size: bytes(f.size) })}</div>`}<pre class="log">${f.content}</pre>`}
  </div>`;
}

// questions.md answered as a form: one text box per "你的意见" cell.
function Questions({ t, base, raw }) {
  const { data: q, error, reload } = useApi(`${base}/questions`, () => false);
  const [answers, setAnswers] = useState({});
  const [busy, setBusy] = useState(false);
  if (error?.status === 422) return html`<div class="notice"><${Icon} name="warning" /><div>${error.message}
    <button class="btn small" onClick=${raw}>${tr('查看原文')}</button></div></div>`;
  if (error) return html`<${ErrorBox} what=${tr('加载 questions.md')} error=${error} retry=${reload} />`;
  if (!q) return html`<div class="loading">${tr('正在加载问题…')}</div>`;
  const val = (r) => answers[r.key] ?? r.answer;
  const filled = q.rows.filter((r) => val(r).trim()).length;
  const pick = (re, fallback) => { const i = q.columns.findIndex((c, j) => j !== q.answer_column && re.test(c)); return i >= 0 ? i : fallback; };
  const qi = pick(/问题|question/i, 1), ai = pick(/建议|suggest/i, -1);  // i18n: reads the agent's questions.md
  const submit = () => {
    setBusy(true);
    api.post(`${base}/questions`, { answers: Object.fromEntries(q.rows.map((r) => [r.key, val(r)])), base_mtime: q.mtime })
      .then(() => toast(tr('已提交：questions.md 已保存，并已通知 agent 继续运行'), { href: taskHref(t.project, t.id, { tab: 'run' }), link: tr('查看输出') }),
        (e) => toast(e.message, { error: true }))
      .finally(() => setBusy(false));
  };
  return html`<form class="questions" onSubmit=${(e) => { e.preventDefault(); submit(); }}>
    ${q.before && html`<${Markdown} text=${q.before} className="doc small" />`}
    <ol class="q-list">${q.rows.map((r) => html`<li class="q-row">
      <div class="q-key mono">${r.key}</div>
      <div class="q-main"><div class="q-text"><${Markdown} text=${r.cells[qi] || ''} /></div>
        ${ai >= 0 && r.cells[ai] && html`<div class="q-advice"><span class="muted small">${tr('建议')}</span><${Markdown} text=${r.cells[ai]} /></div>`}
        <label class="sr-only" for=${`ans-${r.key}`}>${tr('{key} 你的意见', { key: r.key })}</label>
        <${AutoText} id=${`ans-${r.key}`} value=${val(r)} onInput=${(v) => setAnswers({ ...answers, [r.key]: v })} onSubmit=${submit}
          placeholder=${tr('你的意见（留空则按建议处理）')} rows=${1} /></div></li>`)}</ol>
    ${q.after && html`<${Markdown} text=${q.after} className="doc small" />`}
    <div class="row-actions sticky-actions"><span class="muted small">${t.status === 'needs_you'
      ? tr('已填写 {filled} / {total} 项，未填写的项将按建议处理', { filled, total: q.rows.length }) : tr('agent 仍在运行，任务进入待确认后才能提交')}</span>
      <button class="btn primary" type="submit" disabled=${busy || t.status !== 'needs_you'}
        title=${tr('保存 questions.md，并在原会话中通知 agent 继续')}>${tr('提交回答')}</button></div>
  </form>`;
}

// On the report of a task to review: 审核通过 lets the downstream tasks start; 修改意见 goes to the
// agent in the same session, and the task is back in 待审核 when it is done again (the downstream waits meanwhile).
function ReviewBar({ pid, t }) {
  const [text, setText] = useState('');
  const [busy, setBusy] = useState(false);
  const base = paths.task(pid, t.id);
  const cost = t.runs[0]?.cost_usd;
  const fail = (e) => toast(e.message, { error: true });
  const approve = () => api.post(`${base}/actions/approve`).then(() => toast(tr('已审核通过，下游任务可以开始')), fail);
  const feedback = () => {
    if (!text.trim() || busy) return;
    setBusy(true);
    api.post(`${base}/messages`, { text }).then(() => { setText(''); toast(tr('修改意见已发送，agent 将在原会话中修改'), { href: taskHref(pid, t.id, { tab: 'run' }), link: tr('查看输出') }); }, fail)
      .finally(() => setBusy(false));
  };
  const downstream = t.downstream.filter((d) => d.blocking).length;
  return html`<section class="review-bar" aria-label=${tr('审核')}>
    <div class="rb-head"><b>${tr('审核报告')}</b>
      ${t.managed && html`<span class="muted small">${tr('本轮花费 {round} · 累计 {total} / 预算 {budget}', { round: money(cost), total: money(t.cost_usd), budget: money(t.budget_usd) })}</span>`}
      <span class="spacer"></span><button class="btn primary" onClick=${approve}>${tr('审核通过')}</button></div>
    ${downstream > 0 && html`<p class="muted small">${tr('审核通过后，{n} 个下游任务才会开始。', { n: downstream })}</p>`}
    ${t.managed ? html`<div class="reply"><label class="sr-only" for=${`fb-${t.id}`}>${tr('修改意见')}</label>
        <${AutoText} id=${`fb-${t.id}`} value=${text} onInput=${setText} onSubmit=${feedback} rows=${2}
          placeholder=${tr('修改意见：agent 在原会话中修改，改完后重新进入待审核')} />
        <button class="btn" disabled=${busy || !text.trim()} onClick=${feedback}>${tr('发送修改意见')}</button></div>`
      : html`<p class="muted small">${tr('该任务未托管：修改意见请在你的终端中告诉 agent，或先纳入托管。')}</p>`}
  </section>`;
}

// ---------------------------------------------------------------- task settings tab: prompt, frontmatter, dependencies

const agentOf = (f) => ({ model: f.model, effort: f.effort, ultracode: !!f.ultracode, budget_usd: +f.budget,
  extra_args: f.extra.split('\n').map((x) => x.trim()).filter(Boolean) });
// Only what differs from the project's defaults goes into the task file, so that a later change of
// project.yaml still reaches the task (A3).
const overrides = (agent, defaults) => Object.fromEntries(Object.entries(agent)
  .filter(([k, v]) => JSON.stringify(v) !== JSON.stringify(defaults[k])));

// The frontmatter's agent after the form is saved: what differs from the project's defaults, plus the keys the form
// does not show (replay, ...), which saving must not drop.
export function agentFrontmatter(agent, defaults, written = {}) {
  const shown = new Set(['model', 'effort', 'ultracode', 'budget_usd', 'extra_args']);
  const kept = Object.fromEntries(Object.entries(written || {}).filter(([k]) => !shown.has(k)));
  return { ...kept, ...overrides(agent, defaults) };
}

function TaskTab({ pid, t }) {
  const base = paths.task(pid, t.id);
  const board = useApi(paths.board(pid), (c) => c.project === pid);
  // `from` is what the form was filled from, with the file's mtime then. A change of the file from outside (an
  // agent, an editor) refills an untouched form, but never one you are editing: saving it is then refused (A2).
  const [from, setFrom] = useState(() => ({ form: initForm(t), mtime: t.prompt.mtime }));
  const [form, setForm] = useState(from.form);
  const [preview, setPreview] = useState(false);
  const dirty = JSON.stringify(form) !== JSON.stringify(from.form);
  const reset = (x) => { const f = { form: initForm(x), mtime: x.prompt.mtime }; setFrom(f); setForm(f.form); };
  useEffect(() => { if (!dirty) reset(t); }, [t.id, t.prompt.mtime]);
  const started = t.runs.length > 0;
  const types = board.data?.types || [];
  const fail = (e) => toast(e.message, { error: true });
  const running = t.status === 'running';
  const save = async (send) => {
    // a new type or name moves the task (with its workspace); the other fields are saved first
    const moving = form.name !== t.name || form.type !== t.type;
    // only the fields you changed; null removes the key (A3)
    const fm = {};
    for (const k of ['title', 'summary', 'parent']) if (form[k] !== from.form[k]) fm[k] = form[k].trim() || null;
    if (JSON.stringify(form.machines) !== JSON.stringify(from.form.machines)) fm.machines = form.machines.length ? form.machines : null;
    if (form.checks !== from.form.checks) fm.checks = checkList(form.checks);
    if (form.review !== from.form.review) fm.review = form.review || null;
    const agent = agentOf(form);
    if (JSON.stringify(agent) !== JSON.stringify(agentOf(from.form))) fm.agent = agentFrontmatter(agent, board.data?.project.agent || {}, t.frontmatter.agent);
    if (JSON.stringify(form.deps) !== JSON.stringify(from.form.deps)) fm.depends_on = form.deps.length ? depEntries(form.deps) : null;
    const prompt = form.prompt !== from.form.prompt ? form.prompt : undefined;
    if (moving && (t.managed || t.progress) && !await confirmMove(t, `${form.type}/${form.name}`)) return;
    try {
      if (Object.keys(fm).length || prompt !== undefined) {
        const d = await api.put(base, { frontmatter: fm, prompt, base_mtime: from.mtime, send_to_agent: send && !moving });
        const saved = { form: initForm(d), mtime: d.prompt.mtime };
        setFrom(saved);
        setForm(moving ? { ...saved.form, type: form.type, name: form.name } : saved.form);
        if (!moving) return toast(send ? tr('已保存，并已将修改作为消息发送给 agent') : tr('已保存到任务文件'));
      }
      const d = await api.post(`${base}/rename`, { type: form.type, name: form.name });
      go(taskHref(pid, d.id, { tab: 'task' }));
      toast(tr('已移动为 {id}，其他任务中的引用已同步更新', { id: d.id }));
      if (send) api.put(paths.task(pid, d.id), { send_to_agent: true }).catch(fail);
    } catch (e) { fail(e); }
  };
  const set = (k) => (v) => setForm((f) => ({ ...f, [k]: v?.target ? (v.target.type === 'checkbox' ? v.target.checked : v.target.value) : v }));
  return html`<div class="task-tab">
    ${started && html`<div class="notice"><${Icon} name="warning" /><div>${tr('任务已开始：修改 prompt 不会影响进行中的会话（prompt 仅在首条消息中发送一次）。如需让 agent 获知修改，请使用"保存并通知 agent"。')}</div></div>`}
    ${dirty && t.prompt.mtime !== from.mtime && html`<div class="notice"><${Icon} name="warning" /><div>
      ${tr('任务文件在你编辑期间已被修改（{time}），保存会被拒绝。', { time: clock(t.prompt.mtime) })}
      <button class="btn small" onClick=${() => reset(t)}>${tr('载入新版本')}</button>${tr('（放弃你的修改）')}</div></div>`}
    <${Deps} pid=${pid} t=${t} board=${board.data} form=${form} saved=${from.form} set=${set} />
    <section class="form-grid">
      <h3>${tr('任务文件')} <span class="mono small muted">${t.prompt.path}</span></h3>
      <label class="field">${tr('标题')}<input value=${form.title} onInput=${set('title')} /></label>
      <label class="field">${tr('摘要')}<input value=${form.summary} onInput=${set('summary')} placeholder=${tr('一句话说明任务内容')} /></label>
      <div class="field-row">
        <label class="field">${tr('类型')}<select value=${form.type} onChange=${set('type')} disabled=${running}>
          ${types.map((ty) => html`<option value=${ty.id}>${ty.id}</option>`)}</select></label>
        <label class="field">${tr('名称')}<input class="mono" value=${form.name} onInput=${set('name')} disabled=${running} /></label>
      </div>
      <p class="muted small">${running ? tr('任务正在运行：停止后才能修改类型或名称。')
        : tr('修改类型或名称会移动任务文件和工作区，其他任务中对它的引用同步更新。也可以在看板上把卡片拖到其他泳道。')}</p>
      <label class="check"><input type="checkbox" checked=${form.review} onChange=${set('review')} />${tr('完成后需审核（先进入待审核，审核通过后才放行下游）')}</label>
      <${MachinesField} declared=${t.project_machines} value=${form.machines} onChange=${set('machines')} />
      <${ChecksField} value=${form.checks} onInput=${set('checks')} structured=${!checksEditable(t.frontmatter.checks)} />
      <label class="field">${tr('父任务')}<select value=${form.parent} onChange=${set('parent')}>
        <option value="">${tr('（无）')}</option>${(board.data?.tasks || []).filter((x) => x.id !== t.id).map((x) => html`<option value=${x.id}>${x.title} · ${x.id}</option>`)}</select></label>
      <details class="advanced" open=${t.managed}><summary>${tr('agent 参数')}</summary>
        <div class="field-row">
          <label class="field">model<${ModelSelect} value=${form.model} onChange=${set('model')} /></label>
          <${EffortSelect} value=${form.effort} onChange=${(x) => setForm((f) => ({ ...f, ...x }))} />
          <label class="field">${tr('预算（美元）')}<input type="number" min="1" value=${form.budget} onInput=${set('budget')} /></label>
        </div>
        <${UltracodeCheck} value=${form.ultracode} onChange=${(x) => setForm((f) => ({ ...f, ...x }))} />
        <label class="field">${tr('额外的 claude 参数（每行一个，原样传递）')}<textarea class="mono" rows="2" value=${form.extra} onInput=${set('extra')} placeholder="--strict-mcp-config" /></label>
        <p class="muted small">${tr('与项目默认值相同的参数不写入任务文件，以后修改项目默认值时随之生效。')}</p>
      </details>
      <div class="prompt-head"><h3>prompt</h3><div class="seg" role="group" aria-label=${tr('prompt 显示方式')}>
        <button aria-pressed=${!preview} onClick=${() => setPreview(false)}>${tr('编辑')}</button><button aria-pressed=${preview} onClick=${() => setPreview(true)}>${tr('预览')}</button></div></div>
      ${preview ? html`<${Markdown} text=${form.prompt} className="doc prompt-preview" />`
        : html`<${MentionText} class="mono prompt-edit" value=${form.prompt} onInput=${set('prompt')} rows=${10} aria-label="prompt"
          tasks=${(board.data?.tasks || []).filter((x) => x.id !== t.id)} />
          <span class="muted small">${tr('输入 @ 引用本项目的其他任务，被引用的任务成为上游依赖（产出依赖），agent 也会知道去读它的工作区。')}</span>`}
      <div class="row-actions sticky-actions">
        <button class="btn primary" disabled=${!dirty} onClick=${() => save(false)}>${tr('保存')}</button>
        ${started && t.managed && html`<button class="btn" disabled=${!dirty} onClick=${() => save(true)}>${tr('保存并通知 agent')}</button>`}
        ${dirty && html`<button class="btn ghost" onClick=${() => setForm(from.form)}>${tr('放弃修改')}</button>`}
        <span class="muted small">${dirty ? tr('有未保存的修改') : tr('与文件一致')}</span>
      </div>
    </section>
  </div>`;
}

function initForm(t) {
  const a = t.agent || {};
  return { title: t.title || '', summary: t.summary || '', type: t.type, name: t.name, review: !!t.review, parent: t.parent || '', deps: depsOf(t.frontmatter),
    machines: [...(t.machines || [])], checks: checksText(t.frontmatter.checks),
    model: a.model || 'opus', effort: a.effort || 'high', ultracode: a.ultracode === true, budget: a.budget_usd ?? 200, extra: (a.extra_args || []).join('\n'), prompt: t.prompt.body || '' };
}

// The machines a task uses: chosen among those of project.yaml (a name not there any more stays until unticked).
function MachinesField({ declared = [], value = [], onChange }) {
  const names = [...new Set([...declared.map((m) => m.name), ...value])];
  if (!names.length) return html`<div class="field"><span>${tr('机器')}</span><span class="muted small">${tr('项目没有登记机器（在设置页填写）；不选机器的任务不参与机器互斥。')}</span></div>`;
  const toggle = (n) => onChange(value.includes(n) ? value.filter((x) => x !== n) : [...value, n]);
  return html`<div class="field"><span>${tr('机器')}</span><div class="machine-picks">
    ${names.map((n) => { const m = declared.find((x) => x.name === n); return html`<label class="check"><input type="checkbox" checked=${value.includes(n)} onChange=${() => toggle(n)} />
      <span class="mono">${n}</span>${m?.gpus ? html`<span class="muted small">${m.gpus} GPU</span>` : ''}${m?.note ? html`<span class="muted small">${m.note}</span>` : ''}</label>`; })}</div>
    <span class="muted small">${tr('运行中和待确认时占用这些机器；被别的任务占用时，本任务等它们空出来再开始。')}</span></div>`;
}

// Acceptance checks as the form edits them: one command per line. A check with a name or a timeout ({name, run,
// timeout}) is shown as its command; such a list is edited in the task file instead.
export const checksText = (list) => (list || []).map((c) => (typeof c === 'string' ? c : c?.run || '')).join('\n');
export const checksEditable = (list) => (list || []).every((c) => typeof c === 'string');
export const checkList = (text) => { const l = (text || '').split('\n').map((x) => x.trim()).filter(Boolean); return l.length ? l : null; };
function ChecksField({ value, onInput, structured }) {
  return html`<label class="field">${tr('验收检查（每行一条命令，可选）')}<textarea class="mono" rows="3" value=${value} onInput=${onInput} readOnly=${structured}
      placeholder=${'test -s "$AGENT_SYS_WORKSPACE/report.md"'} />
    <span class="muted small">${structured ? tr('这些检查带有名称或超时设置，请在任务文件中修改。')
      : tr('agent 把 progress.md 标为已完成后，agent_sys 在项目目录中逐条运行；有失败就把输出发回 agent 修复（默认最多 2 次），仍失败则转为待确认。可用 $AGENT_SYS_WORKSPACE 等变量。')}</span></label>`;
}

export function depsOf(fm) {
  const d = fm?.depends_on;
  return (d == null ? [] : Array.isArray(d) ? d : [d])
    .map((e) => (typeof e === 'string' ? { task: e, kind: 'input', label: '' } : { task: e?.task, kind: e?.kind || 'input', label: e?.label || '' }))
    .filter((e) => typeof e.task === 'string');
}

// [{task, kind, label}] -> depends_on as written: the short form for a plain input.
export const depEntries = (deps) => deps.map((d) => (d.kind === 'input' && !d.label ? d.task : { task: d.task, kind: d.kind, ...(d.label && { label: d.label }) }));

// Moving a task that has a workspace: what happens, before it does.
export function confirmMove(t, id) {
  const [type, name] = id.split('/');
  return confirmDialog({ title: tr('将「{title}」移动为 {id}？', { title: t.title, id }), ok: tr('移动'),
    body: html`<p>${tr('任务文件移到 {path}，工作区（含会话和运行记录）一起移动。', { path: `tasks/${type}/${name}.md` })}</p>
      <p class="muted small">${tr('其他任务 frontmatter 和 prompt 中对它的引用会同步改为新路径，依赖关系不变。已运行过的任务，下次给 agent 发消息时会告诉它新的路径。')}</p>` });
}

function KindSelect({ value, onChange, label, disabled }) {
  return html`<select class="kind-select" value=${value} onChange=${(e) => onChange(e.target.value)} aria-label=${label} disabled=${disabled}
    title=${EDGE_KINDS[value]?.desc}>${Object.entries(EDGE_KINDS).map(([k, v]) => html`<option value=${k}>${v.label}</option>`)}</select>`;
}

// Upstream tasks to tick, several at once, with a search. chosen: the ids ticked; toggle(id).
function DepPicker({ tasks, chosen, toggle }) {
  const [q, setQ] = useState('');
  const ref = useRef();
  useEffect(() => {  // closes when you click elsewhere or press Esc
    const click = (ev) => { if (ref.current?.open && !ref.current.contains(ev.target)) ref.current.open = false; };
    const key = (ev) => { if (ev.key === 'Escape' && ref.current?.open) { ev.stopPropagation(); ref.current.open = false; } };
    addEventListener('click', click);
    addEventListener('keydown', key, true);
    return () => { removeEventListener('click', click); removeEventListener('keydown', key, true); };
  }, []);
  const s = q.trim().toLowerCase();
  const shown = tasks.filter((x) => !s || `${x.title} ${x.id}`.toLowerCase().includes(s));
  return html`<details class="menu-wrap dep-picker" ref=${ref}>
    <summary class="btn small"><${Icon} name="plus" />${tr('添加上游任务（可多选）')}</summary>
    <div class="dep-pick">
      <input type="search" value=${q} onInput=${(e) => setQ(e.target.value)} placeholder=${tr('搜索标题或名称')} aria-label=${tr('搜索上游任务')} />
      <ul>${shown.map((x) => html`<li><label class="check"><input type="checkbox" checked=${chosen.has(x.id)} onChange=${() => toggle(x.id)} />
        <${StatusMark} t=${x} short /><span class="ellipsis">${x.title}</span><span class="mono small muted">${x.id}</span></label></li>`)}
        ${!shown.length && html`<li class="muted small">${tr('没有可选的任务')}</li>`}</ul>
    </div></details>`;
}

// Upstream tasks, edited with the form (depends_on, and the parent above it); downstream ones, read only (each is
// edited in its own settings). An edge may come from a path in the prompt: its kind can be changed (a depends_on
// entry overrides it), but it goes only with the path.
function Deps({ pid, t, board, form, saved, set }) {
  const deps = form.deps;
  const dep = (id) => deps.find((d) => d.task === id);
  const setDeps = set('deps');
  const setKind = (id, kind) => setDeps(dep(id) ? deps.map((d) => (d.task === id ? { ...d, kind } : d)) : [...deps, { task: id, kind, label: '' }]);
  const toggle = (id) => setDeps(dep(id) ? deps.filter((d) => d.task !== id) : [...deps, { task: id, kind: 'input', label: '' }]);
  const titleOf = (id) => board?.tasks.find((x) => x.id === id)?.title || t.upstream.find((u) => u.task === id)?.title || id;
  const ids = [...new Set([...t.upstream.map((u) => u.task), ...saved.deps.map((d) => d.task), ...deps.map((d) => d.task), form.parent].filter(Boolean))];
  const up = (id) => {
    const u = t.upstream.find((x) => x.task === id), d = dep(id), was = saved.deps.find((x) => x.task === id);
    const parent = !d && form.parent === id, prompt = !d && !parent && !!u?.evidence;
    if (!d && !parent && !prompt && !was && !u) return null;
    const removed = !d && !parent && !prompt;
    const kind = d?.kind || (parent ? 'subtask' : u?.kind || 'input');
    const changed = JSON.stringify(d) !== JSON.stringify(was) || (parent && form.parent !== saved.parent);
    return html`<li class=${`dep ${removed ? 'removed' : ''}`}>
      ${u ? html`<${StatusMark} t=${{ status: u.status }} short />` : html`<span class="tag">${tr('新')}</span>`}
      <a href=${taskHref(pid, id)}>${titleOf(id)}</a>
      ${removed ? html`<span class="tag">${EDGE_KINDS[kind]?.label}</span>`
        : html`<${KindSelect} value=${kind} onChange=${(k) => setKind(id, k)} label=${tr('{task} 的依赖类型', { task: titleOf(id) })} />`}
      ${u && !changed && BLOCKING.has(u.kind) && html`<span class=${`small ${u.satisfied ? 'ok-text' : 'warn-text'}`}>${u.satisfied ? tr('已满足') : tr('未满足')}</span>`}
      ${changed && !removed && html`<span class="small warn-text nowrap">${tr('未保存')}</span>`}
      <span class="small muted ellipsis" title=${u?.evidence}>${removed ? tr('保存后移除')
        : parent ? tr('来自父任务') : d?.label || (u?.evidence ? tr('prompt 原文：{evidence}', { evidence: u.evidence }) : '')}${prompt ? tr('（来自 prompt）') : ''}</span>
      ${removed ? html`<button class="btn ghost small" onClick=${() => (was ? setDeps([...deps, was]) : set('parent')(id))}>${tr('撤销')}</button>`
        : d ? html`<button class="btn ghost small" onClick=${() => toggle(id)}>${tr('移除')}</button>`
          : parent ? html`<button class="btn ghost small" onClick=${() => set('parent')('')}>${tr('移除')}</button>`
            : html`<span class="small muted nowrap" title=${tr('这条依赖来自 prompt 中的任务路径或 @ 引用')}>${tr('移除：删掉 prompt 中的路径或 @ 引用')}</span>`}</li>`;
  };
  const down = (u) => html`<li class="dep">
    <${StatusMark} t=${{ status: u.status }} short /><a href=${taskHref(pid, u.task)}>${u.title}</a>
    <span class="tag">${EDGE_KINDS[u.kind]?.label}</span>
    <span class="small muted ellipsis" title=${u.evidence}>${u.label || (u.evidence ? tr('prompt 原文：{evidence}', { evidence: u.evidence }) : '')}${u.source === 'prompt' ? tr('（来自 prompt）') : ''}</span></li>`;
  const rows = ids.map(up).filter(Boolean);
  const exclude = new Set([t.id, ...t.upstream.filter((u) => !dep(u.task)).map((u) => u.task)]);
  return html`<section class="deps">
    <h3>${tr('上游依赖')} ${rows.length}</h3>
    <ul class="dep-list">${rows}</ul>
    ${!rows.length && html`<div class="muted small">${tr('暂无上游依赖')}</div>`}
    ${board && html`<${DepPicker} tasks=${board.tasks.filter((x) => !exclude.has(x.id))} chosen=${new Set(deps.map((d) => d.task))} toggle=${toggle} />`}
    <h3>${tr('下游任务')} ${t.downstream.length}</h3>
    <ul class="dep-list">${t.downstream.map(down)}</ul>
    ${!t.downstream.length && html`<div class="muted small">${tr('暂无下游任务')}</div>`}
    <p class="muted small">${tr('上游依赖写入本任务 frontmatter 的 depends_on，改完后点"保存"生效；会形成循环的阻塞依赖会被拒绝。prompt 中引用的任务（路径或 @类型/名称）也是依赖：可以改它的类型，移除需删掉 prompt 中的引用。下游任务的依赖在各自的任务配置中修改；待开始的任务也可以不等上游，直接开始。')}</p>
  </section>`;
}

export function NewTaskDrawer({ pid }) {
  const { data: b } = useApi(paths.board(pid), (c) => c.project === pid);
  const [f, setF] = useState(null);
  // what the server makes of the form: name check and the dependencies the task will get (A4)
  const [check, setCheck] = useState({ name_error: '', upstream: [] });
  const [newType, setNewType] = useState(null);
  const [busy, setBusy] = useState(false);
  const close = () => go(hrefWith({ new: null }));
  useEffect(() => {
    if (b && !f) {
      const ty = b.types.find((x) => x.id === 'develop') || b.types[0];
      const a = b.project.agent || {};  // the project's defaults (project.yaml)
      setF({ type: ty?.id || '', name: '', title: '', prompt: ty?.skeleton || '', deps: [], review: !!ty?.review, machines: [], checks: checksText(ty?.checks),
        budget: a.budget_usd ?? 200, model: a.model || 'opus', effort: a.effort || 'high', ultracode: a.ultracode === true, extra: (a.extra_args || []).join('\n') });
    }
  }, [b]);
  useEffect(() => {
    if (!f) return undefined;
    const id = setTimeout(() => api.post(`/api/projects/${pid}/tasks/preview`,
      { type: f.type, name: f.name, prompt: f.prompt, depends_on: depEntries(f.deps) }).then(setCheck, () => {}), 250);
    return () => clearTimeout(id);
  }, [f?.type, f?.name, f?.prompt, JSON.stringify(f?.deps)]);
  useEffect(() => { const k = (ev) => ev.key === 'Escape' && !document.querySelector('dialog[open]') && close(); addEventListener('keydown', k); return () => removeEventListener('keydown', k); }, []);
  if (!b || !f) return html`<aside class="drawer" aria-label=${tr('新建任务')}><div class="drawer-body loading">…</div></aside>`;
  const set = (k) => (v) => setF((x) => ({ ...x, [k]: v?.target ? (v.target.type === 'checkbox' ? v.target.checked : v.target.value) : v }));
  // a type: {id, review, skeleton}; the skeleton replaces the prompt unless you have written in it
  const pickType = (ty) => {
    const old = b.types.find((x) => x.id === f.type);
    setF((x) => ({ ...x, type: ty.id, review: !!ty.review, prompt: !x.prompt.trim() || x.prompt === old?.skeleton ? ty.skeleton || '' : x.prompt,
      checks: x.checks === checksText(old?.checks) ? checksText(ty.checks) : x.checks }));
  };
  const nameErr = f.name ? check.name_error : '';
  const skeletonChecks = b.types.find((x) => x.id === f.type)?.checks || [];
  const create = (mode) => {
    if (!f.name || nameErr) return toast(nameErr || tr('请填写名称'), { error: true });
    setBusy(true);
    const agent = overrides(agentOf(f), b.project.agent || {});
    api.post(`/api/projects/${pid}/tasks`, { type: f.type, name: f.name, prompt: f.prompt, review: f.review, mode, machines: f.machines,
      // the type's checks as written (a check with a name or timeout keeps them) unless you changed them
      checks: f.checks === checksText(skeletonChecks) ? skeletonChecks : checkList(f.checks) || [],
      ...(f.title.trim() && { title: f.title.trim() }),
      ...(f.deps.length && { depends_on: depEntries(f.deps) }),
      ...(Object.keys(agent).length && { agent }) })
      .then((t) => {
        toast(mode === 'draft' ? tr('已保存为草稿：{id}（不会运行，可稍后纳入托管）', { id: t.id })
          : tr('已创建并排队：{id}', { id: t.id }));
        go(taskHref(pid, t.id, { tab: 'task' }));
      }, (e) => toast(e.message, { error: true })).finally(() => setBusy(false));
  };
  return html`<aside class="drawer new-task" aria-label=${tr('新建任务')}>
    <div class="drawer-head"><div class="head-top"><h2>${tr('新建任务')}</h2><span class="spacer"></span>
      <button class="btn icon-only ghost" onClick=${close} aria-label=${tr('关闭（Esc）')}><${Icon} name="x" /></button></div>
      <p class="muted small">${tr('将写入')} <code class="mono">campaigns/${pid}/tasks/${f.type}/${f.name || tr('<名称>')}.md</code></p></div>
    <div class="drawer-body form-grid">
      <div class="field"><span>${tr('类型')}</span><div class="seg wrap" role="radiogroup" aria-label=${tr('类型')}>
        ${b.types.map((ty) => html`<button role="radio" aria-checked=${f.type === ty.id} onClick=${() => pickType(ty)}>${ty.id}</button>`)}
        ${!b.types.some((ty) => ty.id === f.type) && html`<button role="radio" aria-checked="true">${f.type}</button>`}
        <button class="ghost" onClick=${() => setNewType({ id: '', review: false })}><${Icon} name="plus" />${tr('新建类型')}</button></div></div>
      <div class="field-row">
        <label class="field">${tr('名称')}<input class="mono" value=${f.name} onInput=${set('name')} placeholder=${tr('例如 restest_c96')} aria-invalid=${!!nameErr} autofocus />
          ${nameErr && html`<span class="error-text small">${nameErr}</span>`}</label>
        <label class="field">${tr('标题（可选）')}<input value=${f.title} onInput=${set('title')} placeholder=${tr('留空则使用名称')} /></label>
      </div>
      <label class="field">${tr('prompt（已按"{type}"类型的模板预填）', { type: f.type })}<${MentionText} class="mono prompt-edit" value=${f.prompt} onInput=${set('prompt')} rows=${10} tasks=${b.tasks} />
        <span class="muted small">${tr('输入 @ 引用本项目的其他任务，被引用的任务成为上游依赖（产出依赖），agent 也会知道去读它的工作区。')}</span></label>
      ${check.upstream.length > 0 && html`<div class="refs small"><b>${tr('创建后的上游依赖')}</b>${tr('：')}
        ${check.upstream.map((u) => html`<div><span class="mono">${u.task}</span> <span class="tag">${EDGE_KINDS[u.kind]?.label}</span>
          <span class="muted">— ${u.source === 'prompt' ? tr('prompt 中的引用：{evidence}', { evidence: u.evidence }) : tr('选定的上游任务')}</span></div>`)}</div>`}
      <div class="field"><span>${tr('上游任务（可选，可多选）')}</span>
        ${f.deps.map((d) => html`<div class="dep"><span class="ellipsis">${b.tasks.find((x) => x.id === d.task)?.title}</span><span class="mono small muted">${d.task}</span>
          <${KindSelect} value=${d.kind} onChange=${(kind) => set('deps')(f.deps.map((x) => (x.task === d.task ? { ...x, kind } : x)))}
            label=${tr('{task} 的依赖类型', { task: d.task })} />
          <button class="btn ghost small" onClick=${() => set('deps')(f.deps.filter((x) => x.task !== d.task))}>${tr('移除')}</button></div>`)}
        <${DepPicker} tasks=${b.tasks} chosen=${new Set(f.deps.map((d) => d.task))}
          toggle=${(id) => set('deps')(f.deps.some((x) => x.task === id) ? f.deps.filter((x) => x.task !== id) : [...f.deps, { task: id, kind: 'input', label: '' }])} /></div>
      <${MachinesField} declared=${b.machines} value=${f.machines} onChange=${set('machines')} />
      <${ChecksField} value=${f.checks} onInput=${set('checks')} structured=${!checksEditable(skeletonChecks)} />
      <label class="check"><input type="checkbox" checked=${f.review} onChange=${set('review')} />${tr('完成后需审核')}<span class="muted small">${b.types.find((x) => x.id === f.type)?.review
        ? tr('（{type} 类型默认开启）', { type: f.type }) : tr('（{type} 类型默认关闭）', { type: f.type })}</span></label>
      <div class="field-row">
        <label class="field">${tr('预算（美元）')}<input type="number" min="1" value=${f.budget} onInput=${set('budget')} /></label>
        <label class="field">model<${ModelSelect} value=${f.model} onChange=${set('model')} /></label>
        <${EffortSelect} value=${f.effort} onChange=${(x) => setF((y) => ({ ...y, ...x }))} />
      </div>
      <${UltracodeCheck} value=${f.ultracode} onChange=${(x) => setF((y) => ({ ...y, ...x }))} />
      <label class="field">${tr('额外的 claude 参数（每行一个，可选）')}<textarea class="mono" rows="1" value=${f.extra} onInput=${set('extra')} /></label>
      <div class="row-actions sticky-actions">
        <button class="btn primary" disabled=${busy} onClick=${() => create('queue')}>${tr('创建并排队')}</button>
        <button class="btn" disabled=${busy} onClick=${() => create('draft')}>${tr('保存为草稿')}</button>
        <span class="muted small">${tr('创建并排队：纳入托管，上游全部完成且有运行名额时自动开始（没有上游则立即开始；项目关闭了自动开始时需手动开始）。保存为草稿：仅写入任务文件，放入草稿区。')}</span>
      </div>
    </div>
    ${newType && html`<${Dialog} title=${tr('新建类型')} onClose=${() => setNewType(null)}>
      <p class="small">${tr('类型对应')} <code>tasks/</code> ${tr('下的一个目录，也是看板上的一条泳道。')}</p>
      <label class="field">${tr('名称')}<input class="mono" value=${newType.id} onInput=${(e) => setNewType({ ...newType, id: e.target.value })} autofocus placeholder=${tr('例如 benchmark')} /></label>
      <label class="check"><input type="checkbox" checked=${newType.review} onChange=${(e) => setNewType({ ...newType, review: e.target.checked })} />${tr('该类型的任务默认完成后需审核')}</label>
      <div class="dialog-actions"><button class="btn ghost" onClick=${() => setNewType(null)}>${tr('取消')}</button>
        <button class="btn primary" onClick=${() => api.post(`/api/projects/${pid}/types`, newType)
          .then((ty) => { setNewType(null); toast(tr('已新建类型 {id}', { id: ty.id })); pickType(ty); }, (e) => toast(e.message, { error: true }))}>${tr('新建')}</button></div>
    </${Dialog}>`}
  </aside>`;
}
