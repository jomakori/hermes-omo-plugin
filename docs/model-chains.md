# Model chains

A chain is the ordered list of models one agent may be served by, ending at the premium, quota-bounded route. The host's launch carries `model` only and derives the provider itself; per-agent fallback is not native, so the walk is owned here — a retryable classifier plus a cooldown/restore state machine in [`orchestrator/chains.py`](../orchestrator/chains.py).

Set chains per agent under `plugins.entries.omo.settings.chains`; anything omitted falls back to the defaults in [`roster.py`](../roster.py).

```yaml
chains:
  sisyphus: ["<provider>/<model>", "<provider>/<fallback>"]
```

A chain walk is cheap until it is not. Three bounds apply.

## The last hop carries a bounded handoff

Advancing onto the final chain element sends a brief plus explicit artifacts instead of re-issuing the full launch payload verbatim. `escalation_budget_chars` (default `6000`, `0` disables) bounds the goal plus the caller's task context.

The worker contract and persona are constant per agent rather than accumulated payload, so they travel whole — still inside the host's 32,000-character launch-context cap. Earlier hops are unchanged: a cheaper model is handed exactly the brief the primary got.

What this does *not* bound: the host builds a child's system prompt from the launch request and adds its own workspace and project-context blocks, which are not fields of the launch request and so cannot be trimmed from here.

## Attempts are bounded per agent

`max_attempts_per_agent` (default `3`, `0` disables) caps how many times one agent may fail *the same stage* before the engine refuses another attempt. The host's `max_turns` bounds one agent's loop; it does not bound how many times this engine will pay for work that is looping.

The count is per `(agent, stage)` and lives engine-wide, so it survives the fallback state that every dispatch rebuilds from scratch. A cancelled attempt is not a stage failure and does not consume the budget.

## A disconnected client is not re-issued

A `499` — client closed request, client disconnected — is a hard stop:

- the walk does not advance onto the next model,
- the worker is reported `cancelled` rather than `failed`,
- the payload carries `"client_gone": true`.

This holds even when `retry_on_errors` lists `499`: the CLI behind the disconnect cannot deliver an answer, so re-issuing it only spends input tokens nobody can receive.

## Hop telemetry

Every dispatch reports `hop_history`: the hops that were tried, in order, each as `{"model": …, "reason": …}`, and the same list rides on the run row from `status`. The last entry is the exit point of the cascade — `success` names the hop that actually served the request, so a primary that answers is still visible as one entry rather than as silence.

| Reason | Meaning |
|---|---|
| `billing` | a drained account: HTTP 402, `insufficient balance`, `add credits` |
| `rate_limit` | 429 or a quota body |
| `transport` | 5xx, timeouts, connection errors |
| `semantic` | non-retryable failures, abort, context overflow |
| `success` | the hop that served the request |

Billing is its own bucket because it is not a transient: it says the account cannot serve at all, which is exactly the case the rest of the chain exists for.

Two numbers become readable from `hop_history`: the fraction of requests that exit at each hop (how much traffic stops before the premium route), and how much of the failure mass is networking versus the provider refusing (transport versus semantic).
