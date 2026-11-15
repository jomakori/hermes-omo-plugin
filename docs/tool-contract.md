# Tool contract

How `omo` and `omo_task` behave once a dispatch leaves the caller: what blocks, what survives a restart, who can see a run, and which fields are evidence.

## Background dispatch

Dispatch blocks by default; pass `background=true` to get a `run_id` immediately and poll it with `status`. The flag applies to a `tasks=` graph as well as to a single task: the graph is declared, its `run_id` returned, and the schedule runs off the tool call instead of holding it open until the last task settles.

The run registry is **durable**: runs and their workers are checkpointed to `state_path` as they change, so a gateway restart answers `status` from the record rather than from an empty list. A worker whose process is gone is reported `INTERRUPTED` — the record is real, the work it was doing is not verified. Nothing resumes: the caller checks the worktree, branch or PR before re-dispatching the same work.

## A run belongs to the session that dispatched it

One gateway process serves every session, so the run registry is shared — an unfiltered `status` would hand one session another's runs, and `cancel` would let it kill work it did not pay for. Each run therefore records its owner's session id, resolved through the host's own bridge (`gateway.session_context`, the accessor Hermes' tools use: the task-local session ContextVar, with `os.environ` as the fallback), and:

- `status` and `tree` answer for **this session's runs only**; the payload carries `session_id` and `scope`, plus `hidden_runs` when records exist that the caller cannot see.
- `cancel` **refuses** a run belonging to another session. The error names the owning session — `run omo_1a2b3c4d belongs to session ses_… , not the calling session (ses_…); pass all_sessions=true to reach across sessions.`
- `all_sessions=true` is the explicit opt-in that lifts the scope for `status`, `tree` and `cancel` — the escape hatch for a deliberate cross-session inspection or takeover.

Attribution degrades safely: a host with no such bridge (or one whose import fails) records no session id rather than failing the dispatch, and a record written **before** attribution existed has no owner at all — it belongs to no session, so it is hidden from a real session's default view and reachable only under `all_sessions=true`. Nothing raises.

## Claim boundary

Every payload that reports worker activity carries a machine-readable boundary:

```json
"claim_boundary": {
  "boundary": "not_evidence_until_observed",
  "observed": ["status", "model", "cancelled", "error", "client_gone", "result.terminal_state", "result.usage_metadata", "result.tool_execution_summary", "result.error_message"],
  "self_reported": ["result.summary", "result.structured_payload"]
}
```

`observed` is what the engine read from the host's own record of the run. `self_reported`
is what the worker wrote about itself — a claim, not evidence, to be checked against
something the host can see (the working tree, the tests, git) before it is repeated to the
user. A worker that says it refactored a module has not thereby refactored it. Validation
errors (`{"error": "goal is required to dispatch."}`) carry no boundary, because no worker
ran.
