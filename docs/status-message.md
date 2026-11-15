# Live status message

Every run owns one live message in the chat that dispatched it. This page documents the render, the delivery rules, pinning, events and the activity line in full; the [README](../README.md#live-status-message) keeps the short version.

Every run owns **one** live struct in the conversation that dispatched it — one
per `run_id`, never a second. It is **edited in place**: posted once and then
updated as the run progresses, so the one message stays current. The struct only
**moves** when `status_move_interval > 0` (it defaults to `0`, which disables
moving): then a changed render is re-posted below the newest message and the
previous copy deleted, so it follows the conversation instead of being buried by
it. A graph run with four workers still owns a single struct; each worker is one
condensed block inside it. So `status_message_enabled: false` turns the whole
surface off.

```
🏗️ omo · omo_304bf8e5 — shell parity across the fleet · 3 workers

OKT-161 · hephaestus · Deep Agent
- run 🔁 t4 · minimax-m3
  ↳ ✍️ write_file components/shell.rs · ⤵ claude-sonnet-5 (rate limit)
- review ⏳

analysis stage · metis · Plan Consultant
- run ⛔ waiting on prometheus
- review ⏳

architecture · oracle · Architecture / Reasoning
- run ⚠️ gateway restart
- review ✅ momus · problems (cycle 1)
```

**Shape.** The goal is stated **once** in the run heading —
`🏗️ omo · <run_id> — <goal≤60>` (`· N workers` appended at fan-out) — and never
repeated per worker. Each worker is then one block whose first line is
`<label> · <agent display>`, where the display is the roster line from
`roster.AgentSpec.display` (`hephaestus · Deep Agent`) and `<label>` is a
condensed form of the task: the ticket id when one is present, else the first
clause, cut on a word boundary to 24 characters at fan-out above 8 (44 otherwise).
A long label is cut at a word boundary and never ends in an ellipsis — no rendered
line does, apart from the deliberate `…N more` collapse marker.

Each block lists three phases in order — `dispatch`, `run`, `review` — one
`- <phase> <emoji>` row each. The `dispatch` row is dropped once the worker has
started (the run row implies it), and the whole `review` row is dropped when the
run was created with `review: false` (read straight off the payload, never
inferred):

| Emoji | Meaning |
|---|---|
| 🔁 | running — the row also carries `<task_id\|run_id> · <serving model>` |
| ⏳ | pending |
| ✅ | done — a finished run row carries its duration (`22m`) |
| 🔍 | reviewing — `reviewer · run_id · task_id:review` |
| ⛔ | blocked on a dependency — the cause is named (`waiting on prometheus`) |
| ⚠️ | interrupted — the cause is named (`gateway restart`) |
| ❌ | failed — the cause is the worker's error, never an echo of the task |
| ⏸ | cancelled — the cancellation cause |
| ⏭ | skipped — mapped by `STATUS_EMOJI`; no live transition emits it yet |

The renderer's map (`orchestrator/status_message.py`, `STATUS_EMOJI`) is the
source of truth for that table; a status the tracker never produces still gets a
glyph rather than `KeyError`.

The 🔁 `run` row is the only one with a line under it: the worker's **real tool
call**, `  ↳ {emoji} {tool} {target}` (emoji resolved from the host registry with
`registry.get_emoji`, capped at ~48 characters), refreshed on the rotation
cadence and changing when the tool changes. When the chain fell back the hop is
appended on that same line (`· ⤵ claude-sonnet-5 (rate limit)`). Until a call is
observable the rotating `  ↳ cycling: <phrase> (3s)` line stands, so the line is
never blank. Review rows are **folded into the producing worker's block** — the
reviewer and verdict are named on the row (`review ✅ momus · problems (cycle 1)`,
`review 🔍 momus · <run> · <task>:review`), never a block of their own. A message
is capped at Discord's 2000 characters: long block lists collapse with `…N more`,
and only if even the headers overflow are trailing blocks folded into `…N more
agents`.

**Delivery.** The struct is posted on the run's first transition and then
**edited in place**, so one message serves the whole run. Moving is opt-in: with
`status_move_interval` above `0` (default `0`, off), once that cadence elapses a
changed render posts a fresh copy below the newest message, deletes the previous
copy, and persists the new id — in that order, so a failed post leaves the
previous struct in place rather than losing the only live one; between moves a
changed render is edited in place. On a **terminal** state the struct never moves:
the final render is an edit, so the last struct stays exactly where it is. The id
is persisted on the run record (`state_path`), so a gateway restart keeps the same
message; a stored id that is **gone** falls back to posting fresh once, never to a
dead edit loop. A move's delete is best-effort — a platform without a deletion API,
or a failed delete, leaves the previous copy behind (the new id is tracked either
way).

**Pinning.** With `status_pin_message` on (the default), the struct is **pinned**
as soon as it is posted, so it stays at the top of the channel instead of being
buried while the run is live, and is **unpinned exactly once** — the moment the
run reaches a terminal state in which **every worker succeeded**. Any other
outcome leaves it pinned: a run that fails, is blocked, is cancelled, is
interrupted, or is simply still running stays pinned, as does a fan-out where one
worker succeeded and another did not. A move pins the fresh copy and releases the
previous one. The pinned message id is persisted alongside the message id on the
run record (`status_pinned_id`), so an unpin — or the cap eviction below — still
knows which pin is ours after a restart. Idempotence is a latch in the tracker:
once released, a late tick or a duplicated terminal event cannot release it again.

Pinning is best-effort, like the rest of the status surface, and has three
limitations worth stating plainly:

- **Manage Messages is required.** Pinning is a permission, and the bot needs
  **Manage Messages** in the channel (or the thread's parent) to set and clear a
  pin. Without it the platform refuses the pin and it is logged as a warning
  (403 / 50013) and skipped — the status message still posts and edits, it just
  does not stay pinned. Grant the permission, then verify a live pin in Discord;
  nothing here can prove it against the real API.
- **Discord only.** Reaching the message goes through `channel.get_partial_message(id)`
  and the `pin`/`unpin` calls of the Discord client. A platform without that
  capability, or a transport without a reachable client, degrades to a logged
  no-op — never an error that could fail the run.
- **A private coupling.** The adapter exposes no public client, so the transport
  reaches the bot through its private `_client` attribute (a public accessor is
  preferred when one exists). If the host renames `_client`, pinning **fails
  safe**: it logs a debug line and becomes a no-op, while posting and editing keep
  working.

The pin cap (Discord's 50 pins per channel, error `30001`) is handled without
touching anyone else's pins: on a cap refusal, the **oldest pin this plugin owns**
(read off the run records) is released and the pin is retried **exactly once**. If
there is no pin of ours to release, the pin is simply not taken.

**Keeping it alive.** The renderer is pure (`orchestrator/status_message.py`,
run state → exact string) and the throttle/dedupe/move rules are a pure state
machine (`orchestrator/status_tracker.py`, no Discord, no clock — both injected):

- the first render posts; later changed renders edit in place, or — only with
  `status_move_interval > 0` — move past that cadence instead;
- a render whose text is unchanged is skipped entirely (no-op dedupe);
- otherwise edits are throttled to at least `status_edit_interval` (default 2s);
- a throttled change is remembered and flushed on the next tick, so a fast burst
  of transitions never loses the last state;
- the in-progress `run` phrase rotates and the worker's tool call is re-read every
  `status_phrase_interval` (default 3s) so the message stays visibly alive;
- a terminal state **always** forces a final edit, ignoring the throttle — and
  never a move.

**Activity.** The `  ↳` line is read from the worker's own Hermes profile store
(`profiles/<agent>/state.db` → `messages`: `tool_name`, `tool_calls`, `timestamp`)
by `orchestrator/worker_activity.py`. The launch handle names no child session
(`SubagentHandle` carries `subagent_id`/`parent_session_id`/`correlation_id`, and
the child's `sessions` row carries the *parent* id), so the mapping is the
profile's **newest session at launch**, captured once and pinned on the worker;
that is exact for one worker of an agent at a time and approximate when two of the
same agent launch within the same instant. The emoji comes from the host tool
registry at runtime (`registry.get_emoji(tool, default="⚡")`), never a hardcoded
map — the plugin stores no tool→emoji table, so a new tool is rendered the moment
the host registers it. The values the host currently holds for the tools a worker
actually reaches for:

| Tool | Emoji |
|---|---|
| `read_file` | 📖 |
| `write_file` | ✍️ |
| `patch` | 🔧 |
| `search_files` | 🔎 |
| `terminal` | 💻 |
| `skill_manage` | 📝 |
| anything unregistered | ⚡ (the `get_emoji` default) |

Any failure — no session, no DB, a locked or unreadable store — leaves the
canned phrase in place.

Delivery is best-effort by contract: a status message is a courtesy, so a failed
post, move or edit never fails the run it describes. The adapter's HTTP session is bound
to the gateway's event loop, so delivery is **scheduled onto that loop** from the
status worker thread (`asyncio.run_coroutine_threadsafe`) rather than run on a
fresh loop — the same cross-thread hop the host's own dispatch uses. There is **no
plugin-facing "send" verb** (`ctx.platform_actions` exposes only `add_reaction` and
`set_thread_title`), so the transport resolves the adapter through the runner's own
profile-aware, fail-closed `_authorization_adapter` lookup — a documented deviation
from the consent-gated facade, since no capability for this verb exists to grant.
See `orchestrator/gateway_status.py`.

**Events.** Transitions are announced as **bare** event names (the host
namespaces them as `omo:<name>`, so a pre-prefixed name is rejected):

| Event | Emitted at |
|---|---|
| `worker_running` / `worker_succeeded` / `worker_failed` / `worker_cancelled` / `worker_done` | `engine.py` — a worker's status change (`_run_sync`, and the background wrapper) |
| `worker_interrupted` | `engine.py` — a worker recovered as interrupted after a restart, or on shutdown |
| `run_created` | `graph.py` — the run record is written |
| `task_started` / `task_blocked` | `graph.py` — a task is submitted, or blocked on a failed dependency |
| `task_settled` | `graph.py` — a task's future completes |
| `review_verdict` / `reviewer_failed` | `graph.py` — a review returns a verdict, or the reviewer itself fails |
| `run_finished` | `graph.py` — the final payload is built |

A worker event's payload carries the facts the live row needs beyond the ids: the
roster `display`, the serving `model`, the worker's `error` (so a stopped row names
its reason), the chain `hop` it fell back to (empty on success), and the
`activity_session` its tool call is read from. `run_created` carries the `review`
flag the renderer obeys.

