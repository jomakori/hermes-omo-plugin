# Live status message

One run owns one message in the chat that dispatched it — one per `run_id`, never a second, one block per worker.

```
🏗️ omo · omo_304bf8e5 — shell parity across the fleet · 3 workers

OKT-161 · hephaestus · Deep Agent
- run 🔁 t4 · minimax-m3
  ↳ ✍️ write_file components/shell.rs · ⤵ claude-sonnet-5 (rate limit)
- review ⏳

analysis stage · metis · Plan Consultant
- run ⛔ waiting on prometheus
```

## Render

The goal is stated **once** in the run heading — `🏗️ omo · <run_id> — <goal≤60>`, with `· N workers` appended at fan-out — and never repeated per worker. Each worker is one block whose first line is `<label> · <agent display>`, where `<label>` is the ticket id when one is present, else the first clause cut on a word boundary to 24 characters above 8 workers (44 otherwise).

Each block lists three phases in order — `dispatch`, `run`, `review` — one row each. The `dispatch` row is dropped once the worker has started, and the `review` row is dropped when the run was created with `review: false`.

| Emoji | Meaning |
|---|---|
| 🔁 | running — the row also carries `<task_id\|run_id> · <serving model>` |
| ⏳ | pending |
| ✅ | done — a finished run row carries its duration (`22m`) |
| 🔍 | reviewing — `reviewer · run_id · task_id:review` |
| ⛔ | blocked on a dependency — the cause is named |
| ⚠️ | interrupted — the cause is named |
| ❌ | failed — the worker's error, never an echo of the task |
| ⏸ | cancelled — the cancellation cause |
| ⏭ | skipped — mapped by `STATUS_EMOJI`; no live transition emits it yet |

`orchestrator/status_message.py` holds `STATUS_EMOJI` and the renderer is pure (run state → exact string). A status the tracker never produces still gets a glyph rather than `KeyError`.

## Activity line

The 🔁 `run` row is the only one with a line under it: the worker's real tool call, `  ↳ {emoji} {tool} {target}`, emoji resolved from the host registry with `registry.get_emoji`, capped at ~48 characters, and refreshed on the rotation cadence. When the chain fell back, the hop is appended on that same line (`· ⤵ claude-sonnet-5 (rate limit)`). Until a call is observable, a rotating `  ↳ cycling: <phrase> (3s)` line stands, so the line is never blank.

Review rows are **folded into the producing worker's block** — the reviewer and verdict are named on the row, never a block of their own.

A message is capped at Discord's 2000 characters: long block lists collapse with `…N more`, and only if even the headers overflow are trailing blocks folded into `…N more agents`.

The value comes from the worker's own profile store (`profiles/<agent>/state.db` → `messages`: `tool_name`, `tool_calls`, `timestamp`). The launch handle names no child session, so the mapping is the profile's **newest session at launch**, captured once and pinned on the worker: exact for one worker of an agent at a time, approximate when two of the same agent launch in the same instant. Any failure — no session, no DB, a locked store — leaves the canned phrase in place.

## Delivery

The message is posted on the run's first transition and then edited in place.

- A render whose text is unchanged is skipped (no-op dedupe).
- Otherwise edits are throttled to at least `status_edit_interval`; a throttled change is remembered and flushed on the next tick, so a burst never loses the last state.
- The in-progress phrase rotates and the tool call is re-read every `status_phrase_interval`, so the message stays visibly alive.
- A terminal state always forces a final edit, ignoring the throttle — and never a move.

Moving is opt-in: with `status_move_interval` above `0`, a changed render re-posts below the newest message, deletes the previous copy, and persists the new id — in that order, so a failed post leaves the previous struct in place. On a terminal state the struct never moves. The id is persisted on the run record, so a restart keeps the same message; a stored id that is gone falls back to posting fresh once, never to a dead edit loop.

Delivery is best-effort by contract: a failed post, move or edit never fails the run it describes. The adapter's HTTP session is bound to the gateway's event loop, so delivery is scheduled onto that loop from the status thread. There is no plugin-facing "send" verb, so the transport resolves the adapter through the runner's own fail-closed lookup — a documented deviation from the consent-gated facade, since no capability for this verb exists to grant. See [`orchestrator/gateway_status.py`](../orchestrator/gateway_status.py).

## Pinning

With `status_pin_message` on, the message is pinned as soon as it is posted and **unpinned exactly once** — the moment the run reaches a terminal state in which every worker succeeded. Any other outcome leaves it pinned: failed, blocked, cancelled, interrupted, still running, or a fan-out where one worker succeeded and another did not. A move pins the fresh copy and releases the previous one. The pinned id is persisted (`status_pinned_id`), and a latch makes release idempotent.

Three limits, stated plainly:

- **Manage Messages is required.** Without it the platform refuses the pin, which is logged (403 / 50013) and skipped: the message still posts and edits.
- **Discord only.** It reaches the message through `channel.get_partial_message(id)` and the client's `pin`/`unpin`. Anything else degrades to a logged no-op.
- **A private coupling.** The adapter exposes no public client, so the transport uses its private `_client` attribute. If the host renames it, pinning fails safe and posting keeps working.

Discord caps a channel at 50 pins (error `30001`). On a cap refusal, the oldest pin this plugin owns is released and the pin retried exactly once; with no pin of ours to release, the pin is simply not taken.

## Events

Transitions are announced as bare event names (the host namespaces them as `omo:<name>`):

| Event | Emitted at |
|---|---|
| `worker_running` / `worker_succeeded` / `worker_failed` / `worker_cancelled` / `worker_done` | `engine.py` — a worker's status change |
| `worker_interrupted` | `engine.py` — a worker recovered as interrupted, or on shutdown |
| `run_created` | `graph.py` — the run record is written |
| `task_started` / `task_blocked` | `graph.py` — a task is submitted, or blocked on a failed dependency |
| `task_settled` | `graph.py` — a task's future completes |
| `review_verdict` / `reviewer_failed` | `graph.py` — a review returns a verdict, or the reviewer fails |
| `run_finished` | `graph.py` — the final payload is built |

A worker event's payload carries the facts the live row needs beyond the ids: the roster `display`, the serving `model`, the worker's `error`, the chain `hop` it fell back to, and the `activity_session` its tool call is read from. `run_created` carries the `review` flag the renderer obeys.
