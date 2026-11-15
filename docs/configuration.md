# Configuration

Settings live in `plugins.entries.omo.settings` in the host's `config.yaml`. Model names resolve through the host's provider routing, so use the aliases that routing already exposes.

```yaml
plugins:
  enabled:
    - omo
  entries:
    omo:
      settings:
        roles:
          implementer: hephaestus
        chains:
          sisyphus: ["<provider>/<model>", "<provider>/<fallback>"]
        enabled_agents: []
```

Anything omitted falls back to the defaults below, which are the ones compiled into [`roster.py`](../roster.py) and the orchestrator.

| Setting | Default | What it controls |
|---|---|---|
| `enabled_agents` | unset (all) | Restrict the roster to these names; a disabled agent is refused with the list of what is enabled |
| `roles` | `ROLE_ALIASES` | Caller-facing role names → roster entries or categories |
| `chains` | `roster.py` | Per-agent model chains, primary first |
| `max_parallel` | `4` | Tasks in flight for one graph dispatch |
| `max_review_cycles` | `1` | Reviewer passes per task; `0` disables review |
| `require_worker_summary` | `true` | A task that closes without a summary or structured payload is `FAILED` |
| `max_attempts_per_agent` | `3` | Failures of one stage by one agent before the engine refuses; `0` off |
| `max_fallback_attempts` | `3` | Hops the chain walk may take |
| `cooldown_seconds` | `30` | How long a failed primary is skipped |
| `restore_primary_after_cooldown` | `true` | Return to the primary once the cooldown elapses |
| `escalation_budget_chars` | `6000` | Goal plus task context the premium hop receives; `0` disables |
| `worker_timeout_seconds` | `1800` | Hard bound on one child |
| `worker_stall_seconds` | `360` | No new message for this long ⇒ stop the child; `0` disables |
| `worker_activity_slice_seconds` | `20` | How often the watchdog looks at a child |
| `activity_profiles_dir` | `<hermes home>/profiles` | Where the activity line reads tool calls from |
| `state_path` | `~/.omo/runs.json` | Where the run registry is persisted |
| `max_persisted_runs` | `50` | Runs kept in that record; `0` keeps everything |
| `status_message_enabled` | `true` | The live status message |
| `status_pin_message` | `true` | Pin it while the run is live (needs **Manage Messages**) |
| `status_edit_interval` | `2.0` | Minimum seconds between edits (throttle floor) |
| `status_move_interval` | `0.0` | `0` edits in place; above `0` re-posts below the conversation |
| `status_phrase_interval` | `3.0` | Seconds between rotating phrase lines and activity refresh |
| `live_transcript_dir` | `~/.omo/live` | Where per-run JSONL transcripts are written |

Jev's settings are documented in [`jev.md`](jev.md). One host setting matters for the planning pipeline: `delegation.max_spawn_depth` must be raised above its default, because Hermes derives a child agent's role from its depth and ignores any role a caller passes.
