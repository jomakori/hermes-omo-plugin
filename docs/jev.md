# `jev_ask` — advisory decisions

`jev_ask` asks [TypeSafe Jev](https://typesafe.ai) — a small **typed-decision classifier** (not a chat model; it returns probabilities over an option set, never text) — for a judgment and hands the result back as a code-authored envelope. It is **advisory by construction**: Jev answers, code decides, and the tool executes nothing.

Four packs ship (`pack=`):

| Pack | Primitives | State keys |
|---|---|---|
| `route_intent` | `choice` tier (trivial/quick/scoped/exploratory/complex/ambiguous) + `choice` domain | `user_message`, `last_question`, `cwd_basename` |
| `gate_risk` | `noul` irreversible / external_side_effect / destructive / secrets_involved | `action_text`, `dry_run` |
| `pick_skill` | `choice` over a caller-supplied shortlist (`skill_shortlist`, ≤255) | `user_request`, `shortlist` |
| `pick_agent` | `choice` over a candidate map of agents + categories (`agent_candidates`, ≤255; defaults to the enabled roster) | `user_message`, `last_question`, `cwd_basename` |

The envelope is always valid JSON and never raises: `{pack, pack_version, backend, model, status, decisions, usage, cost_usd, latency_ms, notes}`. `status` is `ok` or `unavailable`; on `unavailable` (key missing, timeout, a non-retryable error, or `jev_enabled=false`) the pack's deterministic defaults are returned so the caller branches and continues — nothing blocks. State is **default-deny** (only the pack's allowlisted keys are sent) and **redacted** (bearer tokens, key-like strings, secrets) before it leaves the cluster. Latency is measured client-side — Jev returns none.

Config, under `plugins.entries.omo.settings`:

```yaml
jev_enabled: true                       # false short-circuits to defaults
jev_base_url: "https://api.typesafe.ai"
jev_model: "jev-latest"
jev_timeout_s: 10
jev_api_key_env: "TYPESAFE_AI_API_KEY"  # read from the environment at call time
# jev_thresholds: {destructive: 0.9}    # optional per-primitive overrides
```

Requires the `TYPESAFE_AI_API_KEY` env var. The key is never logged or returned.

## Shadow mode & calibration

Phase 2 lets Jev run **beside** dispatch without touching it. When `jev_shadow_enabled` is on, every `dispatch` fires the configured packs against the same input on a **daemon thread**, records Jev's verdict next to the decision the engine actually made, and returns immediately — the shadow call never adds latency to, or fails, a dispatch, and any error (bad path, full disk, Jev down) is swallowed. Nothing in the routing path reads the result.

The harness runs **`pick_agent`** by default: its candidate map (agents + categories) is the same vocabulary the engine resolves to, so `caller_decision` and Jev's `agent` choice are compared like-for-like rather than across unrelated label spaces.

Each row is one JSON object in a JSONL sink: `{ts, pack, state_hash, jev_decisions, jev_confidence, caller_decision, agreed, latency_ms, model, status}`. `state_hash` is a **sha256 of the input** — raw state, prompts, and the API key are never written.

`jev_report` prints the roll-up: total records, per-pack counts, agreement %, ECE, Brier, and the most recent disagreements (bounded).

```yaml
jev_shadow_enabled: false                            # observation only; default off
jev_shadow_path: "~/.omo/jev-shadow.jsonl"           # JSONL sink
jev_shadow_packs: ["pick_agent"]                     # packs run in shadow (like-for-like with dispatch)
```

ECE and Brier are computed against `agreed` as the 0/1 outcome, so the numbers measure how well Jev's confidence tracks agreement with the engine — the calibration gate before any Phase 3 cutover.

## Authoritative mode (opt-in)

**Default off.** With `jev_routing_enabled: false` — the default — dispatch is byte-identical to before Jev existed. Turning it on lets Jev's `pick_agent` verdict choose the dispatch target, but only inside a deterministic cage:

- **Floor.** An explicit `agent=`/`category=` from the caller always wins; Jev is not consulted for it.
- **Validity.** Jev's pick is accepted only if it names an **enabled** roster agent or a category. An out-of-vocabulary answer (defended against even though Jev can only return a member of the option set) is ignored.
- **Threshold.** The pick must score at least `jev_routing_threshold` (default `0.75`).
- **Fail-open.** Off-spec answers, a timeout (bounded by `jev_timeout_s`), a missing key, or any error fall back to the static resolution unchanged — the static path is the safety net.

Every accepted/rejected decision emits a `jev_route` event naming `source` (`jev`|`static`), `confidence`, and the chosen `target`.

> **Do not enable this until shadow evidence clears the bar.** Run `jev_shadow_enabled` first and confirm the agreement/ECE/Brier numbers show Jev is calibrated on *our* traces for the `pick_agent` pack. Enablement is a config decision, not a code one; the flag exists so a revert is a single config flip.

```yaml
jev_routing_enabled: false    # authoritative dispatch; default off — shadow first
jev_routing_threshold: 0.75   # min pick_agent confidence to override the static target
```
