# Architecture

Every worker launches through the host's subagent lifecycle, so it inherits the host's session record, event stream, transcripts, tool scoping and provider routing. The plugin supplies what the host does not: the roster, the chains, the graph scheduler and the run tree.

```
Hermes session (the only user-facing identity)
  └── omo / omo_task tool call
        └── engine dispatch
              ├── chain resolution      per-agent models, cooldown/restore
              ├── graph scheduler       dependencies, max_parallel, review
              ├── one child per task    host subagent lifecycle, inherited toolsets
              └── run record + status   state_path, live transcript, status message
```

## Decisions worth knowing

- **Per-agent model, not provider.** The host's launch carries `model` only and derives the provider itself. Per-agent fallback is not native either, so it is owned here — a retryable classifier plus a cooldown/restore state machine in `orchestrator/chains.py`.
- **Personas are delivered, not assumed.** A worker's `agents/<name>.md` goes into its launch `context`, wrapped in an authoritative binding preamble, so the definition reaches the model rather than sitting in the repo unread. The host caps a launch's context at 32,000 chars, which every persona fits whole except `sisyphus`, which is truncated with a pointer to the full text. Every persona is also registered as a plugin skill — `skill_view("omo:<name>")` — so the complete definition is always retrievable.
- **No per-agent permission tier.** Hermes derives a child's capabilities from its parent and refuses a launch whose toolsets are not a subset of the parent's. An earlier read-only tier could not be expressed that way, and the `pre_tool_call` guard that stood in for it never fired — it was keyed on a `session_id` the host does not send. It has been removed rather than repaired: every agent runs with the parent's capabilities, and the roster carries no permission field to mislead.
- **The registry is on disk, not in memory.** Every run keeps a record: created, then written again at each worker's terminal state. A restart adopts that record and names what it cannot stand behind — a worker that was still running reads `INTERRUPTED`, with the work it was doing marked unverified. It does not resume: the process is gone, so the caller checks the worktree, branch or PR before re-dispatching the same work.
- **Worker approvals.** Subagent worker threads run non-interactive and refuse dangerous commands by default; ordinary work — files, tests, builds, git — is unaffected.
- **Registration is unconditional.** `register_tool` is required and fails loudly if the host lacks it; `register_command`, `register_skill` and `on_unload` are each attempted on their own, so a host missing one still gets the others. Nothing branches on a host attribute's presence: an attribute that exists but does nothing would send the whole path down a branch that registers nothing while the plugin still reports as enabled.

## The worker contract

Every launch context opens with the internal-worker contract: the worker is told it is not the assistant, that user-facing communication is disabled, and that its findings go back to the orchestrator with what it verified separated from what it assumed. This is prepended to the persona (and delivered even when a roster entry has no persona), so it cannot be lost by editing a prompt.

The clause travels in the launch *context*: a child's system prompt is built by the host and a plugin cannot replace it. A live probe (2026-09-22) asked a worker to name itself and it answered "Hermes", so treat the wording as guidance rather than a guarantee. The guarantee is architectural — a worker has no channel to the user, because the host strips `send_message`, `delegate_task`, `clarify`, `memory` and `cronjob_manage` from child toolsets. Whatever a worker calls itself never reaches a user.
