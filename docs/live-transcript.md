# Live transcripts

One run writes one append-only JSONL file as it progresses, so a caller — or
another agent — can watch it instead of waiting for the final result. It is an
observability side channel: nothing in the run reads it or waits on it.
`orchestrator/live_transcript.py` owns the format, and `dashboard/plugin_api.py`
reads the same files for the web UI.

## Path

```
~/.omo/live/<run_id>.jsonl
```

`DEFAULT_LIVE_DIR` is `~/.omo/live`; the `live_transcript_dir` setting moves it.
The file is created where the run record is created — before the first worker
event — so even a run that never launches a worker has a header line. When the
directory cannot be created, `for_run()` returns `None` and every later write for
that run is a no-op.

## Line shape

One JSON object per line, always the same six fields:

| Field | Type | Meaning |
|---|---|---|
| `seq` | int | 1-based, monotonic per file; resumes from the last valid line when a restart re-attaches to the same path |
| `ts` | float | epoch seconds (`time.time()`) at write time |
| `agent` | string | roster name of the worker the line is about; `""` on run-level lines |
| `task_id` | string or null | the task it belongs to; `null` on run-level lines |
| `kind` | string | one of the five kinds below |
| `text` | string | the note: one line, redacted, at most 500 characters |

Real lines from one run (`·` is written escaped, as `json.dumps` does):

```json
{"seq": 1, "ts": 1794465258.3475263, "agent": "", "task_id": null, "kind": "launch", "text": "shell parity across the fleet"}
{"seq": 2, "ts": 1794465258.347621, "agent": "hephaestus", "task_id": "OKT-161", "kind": "launch", "text": "launch hephaestus \u00b7 Deep Agent claude-sonnet-5"}
{"seq": 3, "ts": 1794465258.3476486, "agent": "hephaestus", "task_id": "OKT-161", "kind": "tool", "text": "write_file components/shell.rs"}
{"seq": 4, "ts": 1794465258.3476636, "agent": "hephaestus", "task_id": "OKT-161", "kind": "result", "text": "succeeded via minimax-m3"}
{"seq": 5, "ts": 1794465258.3476748, "agent": "oracle", "task_id": null, "kind": "error", "text": "worker failed: api_key=<redacted> rejected"}
```

## Kinds

| `kind` | Written when | `text` |
|---|---|---|
| `launch` | the file is created (header), and on a worker's `worker_running` | header: the run's title or goal; worker: `launch <agent display> <model>` |
| `status` | `run_created`, `run_finished`, `task_blocked`, `review_verdict`, `retry_escalated` | the cause or verdict, else the event name |
| `tool` | the watchdog sees a new tool call in the worker's session | `<tool> <target>`, once per distinct call |
| `result` | `worker_succeeded` | `succeeded`, or `succeeded via <hop>` after a fallback |
| `error` | `worker_failed`, `worker_cancelled`, `reviewer_failed` | the error or cause, else the event name |

The mapping is `_transcript_event` in `orchestrator/engine.py`; a kind outside
the five is coerced to `status`. Worker heartbeats are not transcribed — a line
appears only when something changed.

## Redaction

Every `text` value passes through one regex (`_REDACT_RE`) before it is written —
the only path to disk. `api_key`, `token`, `secret`, `password`,
`authorization` and `bearer` (any case, `key: value` or `key=value`) keep the name
and lose the value: `api_key=abc123` becomes `api_key=<redacted>`, and
`Authorization: Bearer <token>` keeps the scheme word only. Text is also
whitespace-collapsed to one line and cut at 500 characters.

## Retention

`prune()` unlinks `*.jsonl` in the live dir whose mtime is older than seven days.
It runs once per transcript construction, is non-recursive, and swallows every
failure.

## Watching one

The run id comes from the dispatch result or `omo action=status` (`run_id`).

```bash
tail -f ~/.omo/live/omo_304bf8e5.jsonl
tail -f ~/.omo/live/omo_304bf8e5.jsonl | jq -r '"\(.agent) \(.kind): \(.text)"'
```

A dispatch returns `live_transcripts` (each worker's `task_id` or roster name
mapped to the run's path) and `live_transcript_hint` (that path as one line to
follow). `omo action=status` adds `transcript_lines` for runs that have one, and
a single run's status carries `transcript_path`.

## Reader API

`orchestrator.live_transcript.LiveTranscript`:

- `read_since(since_seq=0, limit=200)` — events with `seq > since_seq`, oldest
  first, capped at `limit` (`0` = unbounded). A half-written last line is skipped.
- `tail_text(max_lines=40, max_chars=1500)` — the newest lines rendered
  `[agent] kind: text`, trimmed to the newest `max_chars` on overflow.

The dashboard polls the first via
`GET /api/plugins/omo/runs/<run_id>/transcript?since=<seq>&limit=<n>`, which
answers `{events, next_since, active, path}` (default limit 200, max 1000).

## Relation to the run registry

`~/.omo/runs.json` (`orchestrator/store.py`, the `state_path` setting) is the
durable record of runs: state for the newest `max_persisted_runs` (default 50),
rewritten atomically at each change and adopted on restart, each record carrying
its `transcript_path`. The registry says which runs exist and in what state; the
transcript says what one run did, line by line. The registry is bounded by count
and transcripts by age, so a run evicted from the registry can still have its
transcript for up to seven days.

## Failure is never fatal

`write()` catches `OSError` and `ValueError` and logs at debug; `for_run()`
returns `None` instead of raising, so a broken live dir means no transcript
rather than a failed dispatch; `prune()` and the constructor's `mkdir` swallow
their own errors. The engine treats a `None` transcript as "no transcript for
this run" on every read and write, and the dashboard answers an empty feed for a
missing file. A transcript is worth losing; the run it describes is not.
