<div align="center">

<img src=".assets/readme/hero.svg" alt="hermes-omo-plugin — native multi-agent orchestration for Hermes Agent" width="100%">

# hermes-omo-plugin

A native agent fleet for [Hermes Agent](https://github.com/NousResearch/hermes-agent) — named specialists, task graphs, model fallback and live run telemetry, all through the host's own subagent lifecycle.

<!-- SHIELDS_BADGES -->
[![CI](https://img.shields.io/github/actions/workflow/status/jomakori/hermes-omo-plugin/ci.yaml?style=for-the-badge&label=CI&logo=githubactions&logoColor=white&color=06B6D4)](https://github.com/jomakori/hermes-omo-plugin/actions/workflows/ci.yaml)
[![Hermes Agent plugin](https://img.shields.io/badge/Hermes%20Agent-plugin-64748B?style=for-the-badge)](https://github.com/NousResearch/hermes-agent)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-06B6D4?style=for-the-badge&logo=python&logoColor=white)](https://www.python.org)
[![License: MIT](https://img.shields.io/badge/License-MIT-64748B?style=for-the-badge)](LICENSE)
<!-- /SHIELDS_BADGES -->

</div>

## What is this?

A Hermes plugin that gives the host a roster of named specialists — orchestrator, planners, critics, researchers, engineers — dispatches them as subagents, and synthesises their structured results into one reply. Hermes stays the only user-facing identity.

The roster and model are adapted from [oh-my-openagent](#relationship-to-oh-my-openagent), without its runtime: no OpenCode, no Bun, no SDK to install or pin.

## Features

| Feature | What it does |
|---|---|
| **Agent fleet** | 14 specialists and 5 categories, each with a persona and a default model chain |
| **Task graphs** | Dependencies, parallel fan-out, `BLOCKED` on a failed dependency, reconcilers that still run |
| **Owned fallback** | Per-agent chains, cooldown/restore, bounded premium handoff, `hop_history` per dispatch |
| **Review cycles** | `momus` reviews each task; the verdict is `pass`, `problems`, `unparsed` — never silent |
| **Live status** | One message per run, edited in place, showing each worker's model and current tool call |
| **Transcripts** | Append-only JSONL per run, tail-able while it is still live |
| **OMO Runs tab** | `hermes dashboard` shows the run registry and a live worker feed, read-only |
| **Advisory routing** | `jev_ask` returns a probability envelope; shadow mode calibrates before anything routes on it |
| **Durable runs** | Registry survives a gateway restart; `status` and `cancel` reach only your own session |

## Quick Start

1. Install and enable, then restart Hermes so the tools and dashboard tab register:

   ```bash
   hermes plugins install jomakori/hermes-omo-plugin --enable
   hermes plugins list   # confirm 'omo' is loaded
   ```

2. Raise the delegation depth. Hermes derives a child's role from its depth and ignores the role a caller passes, so the default keeps the tree flat:

   ```yaml
   delegation:
     max_spawn_depth: 2   # default 1: a child cannot delegate further
   ```

3. Dispatch, then collect:

   ```json
   {"action": "dispatch", "goal": "harden the sync path", "agent": "hephaestus"}
   ```

   Blocks until the run settles; add `background=true` for a `run_id` and poll with `status`. `omo_task` delegates one subtask by `agent=` or `category=`.

Requires Hermes Agent with plugin and subagent-lifecycle support, and Python 3.11+ (the host's interpreter — no extra runtime dependencies).

Optional: `TYPESAFE_AI_API_KEY` for `jev_ask`, and Discord's **Manage Messages** if the status message should stay pinned. Update with `hermes plugins update omo`, remove with `hermes plugins remove omo`.

## Configuration

Settings live in `plugins.entries.omo.settings`. Model names resolve through the host's provider routing, so use the aliases it already exposes.

| Setting | Default | What it controls |
|---|---|---|
| `enabled_agents` | unset (all) | Restrict the roster to these names |
| `roles` | `ROLE_ALIASES` | Role names → roster entries or categories |
| `chains` | `roster.py` | Per-agent model chains, primary first |
| `max_parallel` | `4` | Tasks in flight for one graph dispatch |
| `max_review_cycles` | `1` | Reviewer passes per task; `0` disables review |
| `worker_stall_seconds` | `360` | No new message for this long ⇒ stop the child; `0` disables |
| `state_path` | `~/.omo/runs.json` | Where the run registry is persisted |
| `status_message_enabled` | `true` | The live status message |

The other 15 settings — chain budgets, watchdog windows, status cadence, transcript directory — are in [docs/configuration.md](docs/configuration.md).

## Tools

| Tool | Purpose |
|---|---|
| `omo` | `dispatch` (one task or a whole graph) / `status` / `tree` / `cancel`, scoped to the calling session unless `all_sessions=true` |
| `omo_task` | Delegate one subtask (`agent=`) or spawn a category worker (`category=`) |
| `jev_ask` | Advisory typed-decision classifier; returns a probability envelope and never executes anything |

## Fleet

[`roster.py`](roster.py) is the source of truth. Each persona is `agents/<name>.md`, delivered per dispatch and loadable as `skill_view("omo:<name>")`.

| Agent | Role | Agent | Role |
|---|---|---|---|
| `sisyphus` | Ultraworker | `librarian` | Research |
| `hephaestus` | Deep Agent | `explore` | Repository Exploration |
| `prometheus` | Plan Builder | `multimodal-looker` | Multimodal Analysis |
| `atlas` | Plan Executor | `sisyphus-junior` | Specialized Execution |
| `metis` | Plan Consultant | `tester` | Test Author |
| `momus` | Plan Critic | `debugger` | Defect Investigator |
| `oracle` | Architecture / Reasoning | `security` | Security Reviewer |

Categories — `quick`, `deep`, `ultrabrain`, `visual-engineering`, `writing` — spawn the execution worker with a category-specific chain. Roles (`explorer`, `planner`, `implementer`, …) resolve through `ROLE_ALIASES` and the `roles` setting; `documenter` points at the `writing` category and `general` at `quick`, the only route to `sisyphus-junior`.

## Project Structure

```
hermes-omo-plugin/
├── agents/                  # one persona per roster entry
├── dashboard/               # OMO Runs tab: manifest, read-only API, built UI
├── docs/                    # contract, config, features, audits
├── jev/                     # typed-decision client, packs, shadow harness
├── omo_tools/               # omo / omo_task / jev_ask handlers
├── orchestrator/            # engine, graph, chains, store, status, transcripts
├── skills/                  # bundled skill docs
├── tests/
├── __init__.py              # register(ctx): tools, commands, skills, dashboard
├── plugin.yaml              # manifest (name: omo, version)
└── roster.py                # fleet: agents, categories, default chains
```

## Documentation

| Document | Covers |
|---|---|
| [docs/configuration.md](docs/configuration.md) | Every setting, default and effect |
| [docs/tool-contract.md](docs/tool-contract.md) | Background dispatch, session scope, claim boundary |
| [docs/task-graphs.md](docs/task-graphs.md) | Graph scheduling, rejection, watchdog, review cycles |
| [docs/model-chains.md](docs/model-chains.md) | Fallback walk, budgets, hop telemetry |
| [docs/status-message.md](docs/status-message.md) | Render, delivery, pinning, activity line, events |
| [docs/live-transcript.md](docs/live-transcript.md) | JSONL shape, kinds, readers, retention |
| [docs/architecture.md](docs/architecture.md) | Launch path, design decisions, worker contract |
| [docs/jev.md](docs/jev.md) | Packs, envelope, shadow calibration, authoritative routing |
| [docs/containment-audit.md](docs/containment-audit.md) | Every chat-emitting path during a run |
| [docs/omo-slim-mechanism-inventory.md](docs/omo-slim-mechanism-inventory.md) | Mechanism inventory against pinned upstream behaviour |

## Security

| Surface | Behaviour |
|---|---|
| Writes | `state_path` (`~/.omo/runs.json`), `~/.omo/live/`, and with shadow on, `~/.omo/jev-shadow.jsonl` |
| Profile reads | `profiles/<agent>/state.db`, to resolve the activity line and detect a stalled child |
| Network | model calls go through the host's routing and credentials; the only direct call is TypeSafe Jev, and only when `jev_ask` is called, shadow is on, or authoritative routing is enabled |
| Credentials | provider keys never touch the plugin; `TYPESAFE_AI_API_KEY` is read from the environment at call time and never logged |
| Chat | the live status message is the only chat writer; workers have no user channel at all |
| Command authority | workers inherit the parent's toolsets and run non-interactive; dangerous commands are refused by the host's approval path |

No telemetry, no phone-home.

## Limits

- **Pinning is Discord-only** and needs **Manage Messages**; otherwise it degrades to a logged no-op.
- **The activity line is approximate under concurrency** — a worker maps to its agent profile's newest session at launch.
- **No resume.** A restart reports a running worker `INTERRUPTED` and does not continue its work.
- **Worker output is a claim.** `claim_boundary` separates what the engine observed from what the worker wrote about itself.
- **Stall detection needs evidence.** Without a readable store, only the hard timeout bounds a child.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| Tree stays flat, orchestrator "cannot delegate" | `delegation.max_spawn_depth` is still at its default — raise it |
| Tools missing after install | the plugin is not in `plugins.enabled`, or Hermes was not restarted |
| An agent is refused | it is not in `enabled_agents`; the error names the enabled ones |
| Status message posts but never pins | the bot lacks **Manage Messages** (403 / 50013, logged and skipped) |
| OMO Runs tab is empty | no run dispatched yet, or `live_transcript_dir` moved the writer off the directory the tab reads |
| `jev_ask` returns `unavailable` | key unset, `jev_enabled: false`, or a timeout; deterministic defaults are returned |

## Development

```bash
python3 -m venv .venv && ./.venv/bin/pip install pytest ruff
./.venv/bin/python -m pytest tests -q
./.venv/bin/ruff check . && ./.venv/bin/ruff format --check .
```

CI runs the suite, both ruff gates, and a guard that fails if a persona prompt still carries an unresolved template token.

## Relationship to oh-my-openagent

This plugin is an adaptation of [oh-my-openagent](https://github.com/code-yeongyu/oh-my-openagent) (formerly oh-my-opencode) for Hermes.

- **Reused:** the agent roster, personas, and the category routing model.
- **Replaced:** OMO's runtime coupling — OpenCode, Bun, the `@opencode-ai` SDK, `opencode.json`, its lifecycle hooks and `omo.jsonc`. The host's own plugin, delegation and config primitives stand in.
- **Not affiliated:** an independent, unofficial adaptation, not produced or endorsed by OMO's authors.

## Attribution

The agent roster, personas and categories originate in [oh-my-openagent](https://github.com/code-yeongyu/oh-my-openagent) by code-yeongyu. The persona prompts under `agents/` are ported from it.

**License note:** upstream OMO is **not open source**. It is source-available under the **Sustainable Use License v1.0** (SUL-1.0), which permits use and modification for personal or internal business purposes but restricts commercial use and redistribution. The files under `agents/` therefore remain under that licence and are not relicensed here — see [`THIRD-PARTY-NOTICES.md`](THIRD-PARTY-NOTICES.md).

## License

[MIT](LICENSE) for everything this repository originates. The persona prompts under `agents/` are derived from oh-my-openagent and stay under its licence — see [`THIRD-PARTY-NOTICES.md`](THIRD-PARTY-NOTICES.md).

The plugin version lives in `plugin.yaml`.
