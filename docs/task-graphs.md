# Task graphs and review cycles

Declare dependent work in one dispatch instead of a sequence of calls.

```json
{"action": "dispatch", "goal": "harden the sync path", "review": true, "tasks": [
  {"id": "recon",  "agent": "explore",  "prompt": "map how sync works today"},
  {"id": "fix",    "agent": "debugger", "prompt": "fix the race", "depends_on": ["recon"]},
  {"id": "test",   "agent": "tester",   "prompt": "cover the race", "depends_on": ["fix"]},
  {"id": "report", "agent": "sisyphus", "prompt": "report what shipped", "depends_on": ["fix"],
   "runs_on_failure": true}
]}
```

## Scheduling

- Independent tasks run in parallel, bounded by `max_parallel`.
- A dependent runs only after its dependencies finish, and receives their summaries as its context — so the task that reports on the work reads what was actually done instead of guessing.
- A dependent of a failed task is reported `BLOCKED` and never launched. The exception is a task marked `"runs_on_failure": true`: a reconciler runs even when a dependency failed, because blocking it would hide the failure from the only task asked to name it.

## Rejection

A declaration is rejected before anything runs if:

- ids repeat,
- a dependency is unknown or points at itself,
- a cycle exists,
- a task carries both `agent` and `category`.

A rejected graph is rendered as `{"error": ..., "rejected": true}` so the caller can fix it and resubmit.

A task that closes cleanly without a summary and without a structured payload is reported `FAILED` (`require_worker_summary`, on by default): an empty result is not evidence of work, and downstream tasks would otherwise reconcile against nothing.

## Stalled children are stopped, not waited out

Every worker used to be awaited in one blocking call for the whole `worker_timeout_seconds` (30 minutes by default), so a child that hung at minute two still held its slot — and the run — until minute thirty. The engine now waits in `worker_activity_slice_seconds` slices and reads the child's own message store:

- while the child keeps writing, nothing happens but a `worker_heartbeat` progress event (at most one a minute, carrying elapsed and idle seconds);
- once the newest message is older than `worker_stall_seconds`, the child is cancelled and the hop fails with `watchdog stopped it: no activity for Ns` — six minutes instead of thirty.

Stall detection is evidence-only. An unpinned session, an unreadable store, or a host that keeps no transcript means no liveness signal, and the hard timeout is again the only bound: a watchdog that guessed would kill slow work.

## Review cycles

`"review": true` runs `momus` over each successful task and re-runs that task with the reviewer's notes, at most `max_review_cycles` times (`0` disables it). Reviewer workers appear in the run tree as `<task>:review` and are folded into the producing worker's block in the live status message.

The reviewer must answer `{"verdict": "pass"|"problems", "problems": [...]}`. A verdict the scheduler cannot parse is treated as a pass rather than as a failure, so an unparseable review can never cause an infinite rewrite.

| `review_verdict` | Meaning |
|---|---|
| `""` | no review ran |
| `pass` | the reviewer passed it, or listed no problems |
| `problems` | the reviewer listed problems; the task was re-run with them |
| `unparsed` | the reviewer answered in a shape the scheduler could not read |
| `reviewer_failed` | the reviewer never completed |

Only `problems` re-runs a task. The other outcomes leave it exactly as it was — but they are named, so "reviewed and clean" is never confused with "the review could not be read". The verdict is derived from the reviewer's own text, so the claim boundary files it as self-reported.
