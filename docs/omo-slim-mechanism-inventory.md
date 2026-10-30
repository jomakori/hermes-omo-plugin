# SL-1: Pinned Mechanism Inventory (§18.2)

Source: `plans/omo-native-hermes-orchestration.md` §18.2.
Repo: `hermes-omo-plugin` (this repo), branch `feat/sl1-mechanism-inventory`.
**Pinned HEAD SHA: `b40421a310c3fcb6392cd717eb43648e6efc6507`**

Scope note: §18.2's own text says upstream (`oh-my-openagent`) line citations are
deliberately omitted because a shallow clone's line numbers drift. This document
instead pins every row to a `path:line` in **this** repo at the SHA above — either
the symbol that implements the "ours today" state (partial mechanisms), or the
gap-adjacent surface that evidences the mechanism's absence (confirmed by a
negative text search across the repo before citing). A citation counts as
**verified** only if (a) the path:line resolves to real content at the pinned SHA,
checked by `read_file`/`search_files` in this session, and (b) it is directly
relevant to the mechanism's stated status. Anything short of that is **unresolved**
and is reported as such rather than forced.

| # | Mechanism | §18.2 status | Citation(s) | Resolves | Note |
|---|---|---|---|---|---|
| 1 | Background supervisor + job board | partial — `Run`/`Worker` persisted | `orchestrator/models.py:120` (`class Worker`), `orchestrator/models.py:248` (`class Run`) | ✅ verified | Both class defs read directly; no reconciliation loop beyond the watchdog (row 3). |
| 2 | Terminal gate: observation tokens, generation fences, revisions, single-use commit token | absent | `orchestrator/engine.py:1004` (`def _await_worker`) | ✅ verified (absence) | Zero matches repo-wide for `generation_fence`, `observation_token`, `commit_token`, `single-use`/`single_use`. `_await_worker` is the function that accepts a finished child's result (`_result_ready(result)` at engine.py:1035) with no token/fence/revision check — cited as the surface the gate is missing from. |
| 3 | Liveness reconciliation: poll, idle/absent grace, stopped-unreconciled vs completed, tombstones | partial — watchdog exists, unwatched cases remain | `orchestrator/engine.py:103` (`class WatchdogStop`), `orchestrator/worker_activity.py:170` (liveness-signal docstring) | ✅ verified | `WatchdogStop` is the only state the watchdog can force; no `stopped-unreconciled`/tombstone state exists in `orchestrator/models.py`. |
| 4 | Concurrency admission: per-model/per-provider/default tiers, queue + pump | absent | `orchestrator/engine.py:349` (`def dispatch`) | ✅ verified (absence) | Searched `admission\|per_model\|per_provider\|semaphore\|queue.*pump\|Queue\(` repo-wide; only hit is `orchestrator/status_notifier.py:79`'s `queue.Queue`, which backs the status-delivery thread, not dispatch admission. `dispatch` is cited as the call with no tier/queue gate in front of it. |
| 5 | Wall-clock supervisor (one-shot deadline, never writes terminal state) | absent | `orchestrator/engine.py:1021-1032` (`_await_worker` deadline loop) | ✅ verified (closest analog, caveated) | A deadline exists (`deadline = started + total`), but it is embedded in `_await_worker` rather than a standalone supervisor, and its timeout return (`"worker hit the {int(total)}s timeout"`) feeds directly into the caller's terminal-state decision — i.e. it is not the "never writes terminal state" shape §18.2 describes. Cited as evidence of the gap, not an implementation. |
| 6 | Foreground fallback conversion (same-provider work runs in caller's turn) | absent | — | ❌ unresolved | `foreground` has zero matches in `orchestrator/*.py` or `chains.py`. `tests/test_wake_caller.py:179` (`test_a_foreground_dispatch_never_wakes`) matches the word but tests wake-caller semantics, not provider-fallback-to-foreground conversion — relevance unconfirmed, not cited as evidence. |
| 7 | Task prompt contract + anti-acknowledgment rule | absent | — | ❌ unresolved | Zero matches for `acknowledg`, `anti-ack`, `task prompt contract`. `orchestrator/chains.py:261`'s `_NO_REPLY_PATTERN` check was inspected as a candidate but its purpose (detecting an empty/no-reply model response for hop classification) was not confirmed to be the same mechanism as an anti-acknowledgment task-prompt contract — not cited. |
| 8 | Cache safety: frozen prefix, tail-only injection, runtime monitor, test gauntlets | absent | — | ❌ unresolved | Zero matches for `cache_safe`, `cache.?safety`, `frozen_prefix`, `tail.?only`, `cache.?monitor`, `prompt_cache` anywhere in the repo. §18.3's D4 (0% cache read, 77 runs) is telemetry evidence from LiteLLM, not a symbol in this repo — no citation available. |
| 9 | Council: independent councillors + synthesizer | partial (momus review) | `roster.py:69` (`"momus": AgentSpec(...)`) | ✅ verified | Momus is a single reviewer persona (`agents/momus.md`), not an independent-councillors-plus-synthesizer structure — matches the "partial" label. |
| 10 | Role routing: specialist bases + custom agents from bases | partial (`roster.py`) | `roster.py:15` (`class AgentSpec`), `roster.py:143` (`CATEGORIES` table) | ✅ verified | One roster table (`AgentSpec` entries + `CATEGORIES` fallback groups), not a bases/derivation model — matches "partial". |
| 11 | Model inheritance (`inheritModelFrom`), ordered per-role chains, no-propagate rules, `restore_primary_after_cooldown` | partial — chains exist, hop broken (§18.3) | `orchestrator/chains.py:298-309` (`class FallbackState`, `restore_primary_after_cooldown` field at `:305`) | ✅ verified | `chain`/`restore_primary_after_cooldown` exist per `FallbackState`; no `inheritModelFrom`/no-propagate concept exists — matches "partial", and D1/D2 (§18.3) are the "hop broken" part. |
| 12 | Presets + runtime preset switching, per-preset agent overrides | absent | — | ❌ unresolved | Zero matches for `preset`/`Preset`/`PRESET` across every `*.py` file in the repo. |

## Summary

- **Verified: 8 / 12** (rows 1, 2, 3, 4, 5, 9, 10, 11)
- **Unresolved: 4 / 12** (rows 6, 7, 8, 12 — mechanisms with no citable symbol in this repo; absence confirmed by repo-wide negative search, but no path:line exists to pin)
- Pinned SHA: `b40421a310c3fcb6392cd717eb43648e6efc6507` (branch `feat/sl1-mechanism-inventory`, forked from `main`)
- No plugin runtime code was modified to produce this inventory; all citations are read-only references into the existing tree.
