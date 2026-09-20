<div align="center">

<img src=".assets/readme/hero.svg" alt="hermes-omo-plugin — native multi-agent orchestration for Hermes Agent" width="100%">

<!-- SHIELDS_BADGES -->
[![CI](https://img.shields.io/github/actions/workflow/status/jomakori/hermes-omo-plugin/ci.yaml?label=CI&logo=githubactions&logoColor=white&color=06B6D4)](https://github.com/jomakori/hermes-omo-plugin/actions/workflows/ci.yaml)
[![Hermes Agent plugin](https://img.shields.io/badge/Hermes%20Agent-plugin-64748B?logoColor=white)](https://github.com/NousResearch/hermes-agent)
[![Python](https://img.shields.io/badge/Python-06B6D4?logo=python&logoColor=white)](https://www.python.org)
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
| `omo` | `dispatch` / `status` / `cancel` — the host's entrypoint |
| `omo_task` | Delegate one subtask (`agent=`) or spawn a category worker (`category=`) |

`omo_task` takes exactly one of `agent=` or `category=`.

## Fleet

`roster.py` is the source of truth for who exists, what each may touch, and its default model chain. Persona prompts live in `agents/<name>.md`.

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

Categories — `quick`, `deep`, `ultrabrain`, `visual-engineering`, `writing` — spawn the execution worker with a category-specific chain.

## How it works

Every worker is launched through the host's subagent lifecycle, so it inherits the host's session record, event stream, transcripts, tool scoping and provider routing. The plugin supplies what the host does not: the roster, per-agent model chains with an owned fallback state machine, per-agent tool scoping, and a run tree the host can print.

A few decisions are worth knowing because they are not obvious:

- **Per-agent model, not provider.** The host's launch carries `model` only and derives the provider itself. Per-agent fallback is not native either, so it is owned here — a retryable classifier plus a cooldown/restore state machine in `orchestrator/chains.py`.
- **Read-only is enforced by hook.** The host's `file` toolset bundles read, write and patch into one unit, and per-tool blocking is rejected at launch, so a `pre_tool_call` guard vetoes writes for read-only sessions.
- **MCP is scoped by toolset.** MCP servers surface as `mcp-<server>` toolsets, so the orchestrator tier can be given the servers the host exposes while the read-only and research tiers are left without them.
- **Worker approvals.** Subagent worker threads run non-interactive and refuse dangerous commands by default; ordinary work — files, tests, builds, git — is unaffected.

## Relationship to oh-my-openagent

This plugin is an adaptation of [oh-my-openagent](https://github.com/code-yeongyu/oh-my-openagent) (formerly oh-my-opencode) for Hermes.

- **Reused:** the agent roster and personas, the category routing model, and the read-only tool policies.
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

The agent roster, personas, categories and read-only policies originate in [oh-my-openagent](https://github.com/code-yeongyu/oh-my-openagent) by code-yeongyu. The persona prompts under `agents/` are ported from it.

**License note:** upstream OMO is **not open source**. It is source-available under the **Sustainable Use License v1.0** (SUL-1.0), which permits use and modification for personal or internal business purposes but restricts commercial use and redistribution. Anyone reusing this repository should review that licence before redistributing the ported prompt text.

## License

Not yet chosen for this repository — see the note under [Attribution](#attribution).

The plugin version lives in `plugin.yaml`.
