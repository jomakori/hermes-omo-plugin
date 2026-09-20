---
name: omo-driven-development
description: Use the native OMO fleet for heavy engineering. Hermes stays the communicator; the fleet does the work and returns structured results.
version: 1.0.0
metadata:
  hermes:
    tags: [omo, delegation, multi-agent, orchestration, subagent]
    related_skills: [subagent-driven-development, writing-plans, code-review]
---

# OMO-Driven Development

Hermes is the sole voice the user hears. The OMO fleet is internal: Hermes classifies, dispatches, verifies, and reports. A worker never addresses the user.

## Route by size

| Task | Move |
|---|---|
| < 3 tool calls, known answer | do it yourself |
| one focused, non-coding subtask | `delegate_task` (Hermes primitive) |
| multi-file feature, refactor, bugfix, tests | `omo` |

## Dispatch

```
omo(action="dispatch", goal="...", agent="hephaestus" | category="deep", context="<conventions/memory>")
omo(action="status", run_id="omo_ab12cd34")
omo(action="cancel", run_id="omo_ab12cd34")
```

## Planning loop

```
Hermes classifies
  → Metis      (clarify scope, read-only)          only when the request is ambiguous
  → Oracle     (architecture, read-only)           only when the design is undecided
  → Prometheus (plan, read-only)
  → Momus      (critique the plan, read-only)
  → present to the USER for approval
  → Hephaestus / category workers  (execute)
  → verify → Hermes synthesises → user
```

Never skip the user approval gate for Standard or Complex work.

## Choosing an agent

| Need | Agent |
|---|---|
| codebase search | `explore` |
| external docs / OSS research | `librarian` |
| architecture or hard reasoning | `oracle` |
| implementation | `hephaestus` |
| plan | `prometheus` |
| plan critique | `momus` |
| ambiguity / pre-planning | `metis` |
| execute an existing plan | `atlas` |
| images / PDFs / diagrams | `multimodal-looker` |
| small, well-scoped, cheap | `category="quick"` |

`category=` spawns `sisyphus-junior` with that category's model chain; `agent=` targets a named agent. Exactly one, never both.

## After every dispatch

Read the returned `status`; on `failed`, read `error` (the engine may already have walked the fallback chain). Verify the result against the request, then report to the user in Hermes' voice.
