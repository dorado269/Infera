# web: agent_sys's front end

No build step: Preact + htm (`vendor/`), ES modules loaded by the browser as they are; reload after a change. Served
by `agent-sys-v2 serve` (same origin, the CSP allows only `'self'`). Ported from conch v1.0.1; what agent_sys adds is
the ledger page, the machines and the acceptance checks.

| File | Content |
|---|---|
| `index.html`, `boot.js`, `style.css` | The page skeleton; `boot.js` is a plain script (old browsers parse it too): applies the theme before the first paint (dark unless light or the system's was picked), and if the app's scripts fail or have not started after 15 s, shows why on the page; all styles (tokens such as colors first) |
| `i18n.js`, `i18n_en.js` | The UI language and the English dictionary: texts are written in Chinese and looked up in `i18n_en.js` for English (`t('等待上游任务：{tasks}', { tasks })`; an entry may be a function, for plurals). The language comes from `<html lang>`; switching it saves the setting and reloads the page. `test/i18n.test.js` checks that every `t()` text has its English |
| `api.js` | **The only data layer**: `api.get/post/put(path, body)`, `api.onChange` (pushed changes over one SSE per page, closed while the page is hidden; a run view is told its run has new events and fetches them), `api.rawUrl`. Paths and response shapes: `agent_sys_v2/server.py` |
| `ui.js` | Status vocabulary and icons, formatting, Markdown (marked + DOMPurify: what agents write may not carry scripts, forms, controls or styles), dialogs, toasts, `useApi`, and choosing a model: `useModels` (`/api/models`, fetched once) and `ModelSelect` (a model, an alias with what it stands for here, or any other name typed in), `MentionText` (a prompt editor where `@` lists the project's tasks and writes `@<type>/<name>`), ... |
| `app.js` | Routes, the top bar (with the language switch), the home page (what needs you, running tasks, machines in use, projects), archived projects, the settings page (a campaign's goal, machines and ledger metric) |
| `knowledge.js` | The rules page (the injected parts and their sizes, editing and creating global / project rules, the read-only agent_sys protocol in the language injected) and the knowledge page (accept / rewrite as a rule / discard candidates, the libraries and INDEX) |
| `cards.js` | The Needs you and In review cards (shared by the home page and the board's pending column): one line by default, the main button chosen by the reason, "Reply" opens an input box; continue all interrupted tasks, mark all as seen |
| `board.js` | The board: the campaign's goal and ledger chips, the machines strip (who holds which machine, held idle), the layout of lanes x time rows and the dependency lines, the pending summary, the toolbar and the "View" popover, the canvas (drag a lane title to reorder the lanes, a card to another lane to change its type), the cards' badges (machines, checks, result delta), the task list |
| `ledger.js` | The ledger page: the primary metric, baseline and best, the trajectory (one dot per result, its A/B decision as shape and color, the baseline dashed, the best so far as a step line), the results table and the malformed files; `LedgerChips` for the board's head |
| `task.js` | The task drawer (header with machines and the checks' outcome, "join them all" for the drafts the task wrote; Workspace tab: the acceptance checks view, the report's handoff list, answering questions.md's table, reviewing the report; Task settings tab: upstream tasks, type and name, machines, checks) and the new-task drawer (the type's checks prefilled) |
| `run.js` | The Run output tab: events.jsonl (slim, from its end, earlier pages on scroll-up; cut texts fetched when opened) folded into a readable stream (the acceptance checks as lines of their own), the status strip, the input box |
| `test/` | Unit tests of the pure functions (`statusLine`, the @ mention helpers, `layout`, `edgePaths`, `fold`, the cards' main button, the ledger's numbers and ticks, the machines and checks form helpers) and the check of the i18n dictionary: `node --test src/agent_sys_v2/web/test/*.test.js` |
| `vendor/` | preact 10.29.8 (hooks' `from "preact"` changed to a relative path), htm 3.1.1, marked 14.1.4 (18.x uses newer syntax such as regex lookbehind that old Safari cannot parse), DOMPurify 3.4.17; licenses in `vendor/licenses/` |

Browsers: Chrome / Edge 80+, Firefox 98+, Safari 15.4+ (`<dialog>`, private class fields, ...).

To open it: `agent-sys-v2 serve`, then on your computer `ssh -N -L 8765:/tmp/agent_sys-<uid>/agent_sys.sock <this machine>`
and open http://localhost:8765.
