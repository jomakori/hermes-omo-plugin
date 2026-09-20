# hermes-omo-plugin

Native OMO (Oh My OpenAgent) multi-agent orchestration for [Hermes Agent](https://github.com/NousResearch/hermes-agent).

It exists so Hermes can run a fleet of specialised agents — an orchestrator, planners, reviewers, researchers and engineers — without a second agent runtime beside it. Every worker is a Hermes subagent launched through Hermes' own lifecycle, so sessions, events, transcripts, tool scoping and provider routing come from the host rather than a parallel stack.

## Why

The fleet previously ran behind an OpenCode server reached through a delegation plugin: a forked plugin, a dedicated server pod, and a third-party npm harness, each versioned and reconciled separately. Hermes now exposes the primitives that layer was emulating, so the fleet lives here instead and nothing sits between Hermes and its subagents.

Hermes remains the only user-facing identity. Workers return structured results; the host synthesises and speaks.

## Fleet

`roster.py` is the source of truth for who exists, what each may touch, and its default model chain. Each agent's persona prompt lives in `agents/<name>.md`.

| Agent | Role | Access |
|---|---|---|
| `sisyphus` | Ultraworker | orchestrator |
| `hephaestus` | Deep Agent | engineering |
| `prometheus` | Plan Builder | read-only |
| `atlas` | Plan Executor | orchestrator |
| `metis` | Plan Consultant | read-only |
| `momus` | Plan Critic | read-only |
| `oracle` | Architecture / Reasoning | read-only |
| `librarian` | Research | read-only |
| `explore` | Repository Exploration | read-only |
| `multimodal-looker` | Multimodal Analysis | read-only |
| `sisyphus-junior` | Specialized Execution Worker | category spawns only |

Categories (`quick`, `deep`, `ultrabrain`, `visual-engineering`, `writing`) spawn the execution worker with a category-specific chain.

## Tools

| Tool | Purpose |
|---|---|
| `omo` | `dispatch` / `status` / `cancel` — Hermes' entrypoint |
| `omo_task` | Delegate one subtask (`agent=`) or spawn a category worker (`category=`) |

`omo_task` takes exactly one of `agent=` or `category=`.

## Requirements

- Hermes Agent with plugins enabled.
- A model provider the host can reach. The reference deployment routes every alias through a LiteLLM gateway, but the plugin only passes model names through to the host's routing.

## Install

Clone into the host's plugins directory, then enable it:

```bash
git clone https://github.com/jomakori/hermes-omo-plugin.git "${HERMES_HOME}/plugins/omo"
```

```yaml
plugins:
  enabled:
    - omo
```

## Configuration

Settings are read through Hermes' plugin config — `plugins.entries.omo.settings`. Model names resolve through the host's provider routing, so use whatever aliases it exposes.

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
        chains:
          sisyphus:
            - "<provider>/<model>"
            - "<provider>/<fallback>"
```

Anything omitted falls back to the defaults in `roster.py`.

## Design notes

- **Per-agent model, not provider.** Hermes' subagent launch carries `model` only; the provider is derived. Per-agent fallback is therefore owned here — a retryable classifier plus a cooldown/restore state machine in `orchestrator/chains.py`.
- **Read-only is enforced by hook, not toolsets.** Hermes' `file` toolset bundles read, write and patch together, and per-tool blocking is rejected at launch, so a `pre_tool_call` guard vetoes writes for read-only sessions (`orchestrator/guards.py`).
- **MCP is scoped by toolset.** MCP servers surface as `mcp-<server>` toolsets, so plane/github access is granted to the orchestrator tier and withheld from the read-only and research tiers.
- **Nesting needs `delegation.max_spawn_depth`.** Hermes derives a child's role from its depth and ignores any role a caller passes, so at the default the whole tree is flat and the orchestrator cannot delegate at all. Raise it in the host config.
- **Worker approvals.** Subagent worker threads run non-interactive: dangerous commands are denied unless the host opts in via `delegation.subagent_auto_approve`. Ordinary work — files, tests, builds, git — is unaffected.

## Development

```bash
python3 -m venv .venv && ./.venv/bin/pip install pytest ruff
./.venv/bin/python -m pytest tests -q
./.venv/bin/ruff check . && ./.venv/bin/ruff format --check .
```

CI runs the test suite on the Python versions in the workflow, plus ruff check/format and a guard that fails if a persona prompt carries an unresolved template token.

## Layout

```text
plugin.yaml            manifest
__init__.py            entrypoint: register()
roster.py              agent roster — roles, tool scopes, model chains
orchestrator/          engine, model-chain/fallback resolution, guards
tools/                 omo and omo_task schemas and handlers
agents/                per-agent persona prompts
skills/                bundled skill
tests/                 unit tests
```

The plugin version lives in `plugin.yaml`.
