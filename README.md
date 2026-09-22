<div align="center">

<img src=".assets/readme/hero.svg" alt="hermes-omo-plugin — native multi-agent orchestration for Hermes Agent" width="100%">

<!-- SHIELDS_BADGES -->
[![CI](https://img.shields.io/github/actions/workflow/status/jomakori/hermes-omo-plugin/ci.yaml?label=CI&logo=githubactions&logoColor=white&color=06B6D4)](https://github.com/jomakori/hermes-omo-plugin/actions/workflows/ci.yaml)
[![Hermes Agent plugin](https://img.shields.io/badge/Hermes%20Agent-plugin-64748B?logoColor=white)](https://github.com/NousResearch/hermes-agent)
[![Python](https://img.shields.io/badge/Python-06B6D4?logo=python&logoColor=white)](https://www.python.org)
[![License: MIT](https://img.shields.io/badge/License-MIT-64748B)](LICENSE)
<!-- /SHIELDS_BADGES -->

</div>

# hermes-omo-plugin

Multi-agent orchestration for [Hermes Agent](https://github.com/NousResearch/hermes-agent). It gives the host a roster of named specialist agents — an orchestrator, planners, critics, researchers and engineers — and runs them as Hermes subagents through the host's own lifecycle. Hermes stays the only user-facing identity: workers return structured results, and the host synthesises and speaks.

The roster and the orchestration model are inspired by [oh-my-openagent](#relationship-to-oh-my-openagent) (OMO). What differs is the substrate — no second runtime to install, pin or reconcile.

## Install

```bash
hermes plugins install jomakori/hermes-omo-plugin
hermes plugins enable omo
```

Or clone it into the host's plugins directory and enable it:

```bash
git clone https://github.com/jomakori/hermes-omo-plugin.git "${HERMES_HOME:-$HOME/.hermes}/plugins/omo"
hermes plugins enable omo
```

## Configuration

Settings are read from `plugins.entries.omo.settings` in the host's `config.yaml`. Model names resolve through the host's provider routing, so use the aliases that routing already exposes.

```yaml
plugins:
  enabled:
    - omo
  entries:
    omo:
      settings:
        mcp_enabled: true
        max_fallback_attempts: 3
        cooldown_seconds: 30
        restore_primary_after_cooldown: true
        max_parallel: 4          # tasks in flight for one graph dispatch
        max_review_cycles: 1     # reviewer passes per task; 0 disables review
        enabled_agents: []       # empty = the whole roster; list names to restrict it
        roles:                   # caller-facing role names -> roster entries
          implementer: hephaestus
        chains:
          sisyphus:
            - "<provider>/<model>"
            - "<provider>/<fallback>"
```

Anything omitted falls back to the defaults in `roster.py`.

One host setting matters for the planning pipeline: Hermes derives a child agent's role from its depth and ignores any role a caller passes, so `delegation.max_spawn_depth` must be raised above its default or the tree stays flat and the orchestrator cannot delegate.

## Tools

| Tool | Purpose |
|---|---|
| `omo` | `dispatch` (one task or a whole graph) / `status` / `tree` / `cancel` |
| `omo_task` | Delegate one subtask (`agent=`) or spawn a category worker (`category=`) |

`omo_task` takes exactly one of `agent=` or `category=`.

Dispatch blocks by default; pass `background=true` to get a `run_id` immediately and poll it with `status`. The run registry is **in-memory and scoped to the session** — `status` lists only runs dispatched in the current one, so history does not survive a session boundary.

Every payload that reports worker activity carries a machine-readable boundary:

```json
"claim_boundary": {
  "boundary": "not_evidence_until_observed",
  "observed": ["status", "model", "cancelled", "error", "result.terminal_state", "result.usage_metadata", "result.tool_execution_summary", "result.error_message"],
  "self_reported": ["result.summary", "result.structured_payload"]
}
```

`observed` is what the engine read from the host's own record of the run. `self_reported`
is what the worker wrote about itself — a claim, not evidence, to be checked against
something the host can see (the working tree, the tests, git) before it is repeated to the
user. A worker that says it refactored a module has not thereby refactored it. Validation
errors (`{"error": "goal is required to dispatch."}`) carry no boundary, because no worker
ran.

## Task graphs

Work with dependencies goes in one call instead of a sequence of dispatches:

```json
{"action": "dispatch", "goal": "harden the sync path", "review": true, "tasks": [
  {"id": "recon",  "agent": "explore",  "prompt": "map how sync works today"},
  {"id": "fix",    "agent": "debugger", "prompt": "fix the race", "depends_on": ["recon"]},
  {"id": "test",   "agent": "tester",   "prompt": "cover the race", "depends_on": ["fix"]},
  {"id": "docs",   "agent": "writing",  "prompt": "note the fix", "depends_on": ["fix"]}
]}
```

Independent tasks run in parallel, bounded by `max_parallel`; a dependent runs only
after its dependencies succeed; a dependent of a failed task is reported `BLOCKED`
and never launched, so a failure cannot silently feed a broken input downstream.
A declaration is rejected before anything runs if ids repeat, a dependency is
unknown or points at itself, a cycle exists, or a task carries both `agent` and
`category` — a rejected graph is rendered as `{"error": ..., "rejected": true}` so
the caller can fix it and resubmit.

## Review cycles

`"review": true` runs `momus` over each successful task and re-runs that task with
the reviewer's notes, at most `max_review_cycles` times (`0` disables it). The
reviewer must answer `{"verdict": "pass"|"problems", "problems": [...]}`; a verdict
the scheduler cannot parse is treated as a pass rather than as a failure, so an
unparseable review can never cause an infinite rewrite. Reviewer workers appear in
the run tree as `<task>:review`.

Every reviewed task reports what the review actually concluded, in
`review_verdict`:

| Value | Meaning |
|---|---|
| `""` | no review ran |
| `pass` | the reviewer passed it, or listed no problems |
| `problems` | the reviewer listed problems; the task was re-run with them |
| `unparsed` | the reviewer answered, but in a shape the scheduler could not read |
| `reviewer_failed` | the reviewer never completed |

Only `problems` re-runs a task. The other outcomes leave it exactly as it was —
but they are named, so "reviewed and clean" is never confused with "the review
could not be read". The verdict is derived from the reviewer's own text, so the
claim boundary files it as self-reported.

## Role names

Callers can ask for a role instead of a codename — `explorer`, `researcher`,
`planner`, `implementer`, `tester`, `debugger`, `reviewer`, `security`,
`documenter`, `general` — resolved through `ROLE_ALIASES` and overridable with the
`roles` setting. `documenter` routes to the
`writing` category and `general` to `quick`, so an alias may point at a category
as well as an agent — which is the only way to reach `sisyphus-junior`, a worker
that refuses to be a direct target. Setting `enabled_agents` restricts the
roster; a disabled agent is refused with the list of what is enabled.

## Worker contract

Every launch context opens with the internal-worker contract: the worker is told it
is not the assistant, that user-facing communication is disabled, and that its
findings go back to the orchestrator with what it verified separated from what it
assumed. This is prepended to the persona (and delivered even when a roster entry
has no persona), so it cannot be lost by editing a prompt — the host also never
gives a worker a channel to the user, so the contract states a fact rather than
asking for compliance.

The clause travels in the launch *context*: a child's system prompt is built by the
host and a plugin cannot replace it. A live probe (2026-09-22) asked a worker to
name itself and it answered "Hermes", so treat the wording as guidance rather than a
guarantee. The guarantee is architectural — a worker has no channel to the user, so
whatever it calls itself never reaches one.
## Fleet

`roster.py` is the source of truth for who exists and its default model chain. Each agent's persona is `agents/<name>.md`, delivered per dispatch (see below) and loadable in full as `skill_view("omo:<name>")`.

| Agent | Role |
|---|---|
| `sisyphus` | Ultraworker |
| `hephaestus` | Deep Agent |
| `prometheus` | Plan Builder |
| `atlas` | Plan Executor |
| `metis` | Plan Consultant |
| `momus` | Plan Critic |
| `oracle` | Architecture / Reasoning |
| `librarian` | Research |
| `explore` | Repository Exploration |
| `multimodal-looker` | Multimodal Analysis |
| `sisyphus-junior` | Specialized Execution Worker |
| `tester` | Test Author |
| `debugger` | Defect Investigator |
| `security` | Security Reviewer |

Categories — `quick`, `deep`, `ultrabrain`, `visual-engineering`, `writing` — spawn the execution worker with a category-specific chain.

## How it works

Every worker is launched through the host's subagent lifecycle, so it inherits the host's session record, event stream, transcripts, tool scoping and provider routing. The plugin supplies what the host does not: the roster, per-agent model chains with an owned fallback state machine, a task-graph scheduler, and a run tree the host can print.

A few decisions are worth knowing because they are not obvious:

- **Per-agent model, not provider.** The host's launch carries `model` only and derives the provider itself. Per-agent fallback is not native either, so it is owned here — a retryable classifier plus a cooldown/restore state machine in `orchestrator/chains.py`.
- **Personas are delivered, not assumed.** A worker's `agents/<name>.md` goes into its launch `context`, wrapped in an authoritative binding preamble, so the definition actually reaches the model rather than sitting in the repo unread. The host caps a launch's context at 32,000 chars, which every persona fits whole except `sisyphus`, which is truncated with a pointer to the full text. Every persona is also registered as a plugin skill — `skill_view("omo:<name>")` — so the complete definition is always retrievable.
- **No per-agent permission tier.** Hermes derives a child's capabilities from its parent and refuses a launch whose toolsets are not a subset of the parent's. An earlier read-only tier could not be expressed that way, and the `pre_tool_call` guard that stood in for it never fired — it was keyed on a `session_id` the host does not send. It has been removed rather than repaired: every agent runs with the parent's capabilities, and the roster carries no permission field to mislead.
- **Worker approvals.** Subagent worker threads run non-interactive and refuse dangerous commands by default; ordinary work — files, tests, builds, git — is unaffected.
- **Registration is unconditional.** `register_tool` is required and fails loudly if the host lacks it; `register_command`, `register_skill` and `on_unload` are each attempted on their own, so a host missing one still gets the others. Nothing branches on a host attribute's presence: an attribute that exists but does nothing would send the whole path down a branch that registers nothing while the plugin still reports as enabled.

## Relationship to oh-my-openagent

This plugin is an adaptation of [oh-my-openagent](https://github.com/code-yeongyu/oh-my-openagent) (formerly oh-my-opencode) for Hermes.

- **Reused:** the agent roster and personas, and the category routing model.
- **Replaced:** OMO's runtime coupling. OMO ships as an OpenCode plugin — OpenCode and Bun, the `@opencode-ai` SDK, an `opencode.json` plugin entry, its lifecycle hooks and its `omo.jsonc` config surface. Here the host's own plugin, delegation and config primitives stand in for all of it, so the same roster runs with nothing extra to install.
- **Not affiliated:** this is an independent, unofficial adaptation and is not produced or endorsed by OMO's authors.

## Development

```bash
python3 -m venv .venv && ./.venv/bin/pip install pytest ruff
./.venv/bin/python -m pytest tests -q
./.venv/bin/ruff check . && ./.venv/bin/ruff format --check .
```

CI runs the suite, both ruff gates, and a guard that fails if a persona prompt still carries an unresolved template token.

## Attribution

The agent roster, personas and categories originate in [oh-my-openagent](https://github.com/code-yeongyu/oh-my-openagent) by code-yeongyu. The persona prompts under `agents/` are ported from it.

**License note:** upstream OMO is **not open source**. It is source-available under the **Sustainable Use License v1.0** (SUL-1.0), which permits use and modification for personal or internal business purposes but restricts commercial use and redistribution. The files under `agents/` therefore remain under that licence and are not relicensed here — see [`THIRD-PARTY-NOTICES.md`](THIRD-PARTY-NOTICES.md).

## License

[MIT](LICENSE) for everything this repository originates. The persona prompts under `agents/` are derived from oh-my-openagent and stay under its licence — see [`THIRD-PARTY-NOTICES.md`](THIRD-PARTY-NOTICES.md).

The plugin version lives in `plugin.yaml`.
