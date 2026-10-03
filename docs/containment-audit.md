# Containment audit — chat-emitting paths while a run is active

Read-only audit for spec **T6**. Question: can anything other than the one live
status struct put a message into a chat while an OMO run is in flight? Every path
the plugin owns is listed with whether it is gated, and the gate is named.

Scope: the `omo` plugin (`/opt/data/wt/omo-status-msg`) plus the host code it
calls (`/opt/hermes`). No host behaviour was modified.

| # | Path | Emits chat? | Gated? | Gate / evidence |
|---|---|---|---|---|
| 1 | Live status struct — `orchestrator/gateway_status.py` (`post`/`edit`/`move`) | **Yes** | **Yes, by design** | The only plugin-side chat writer. Only reached when a notifier is attached (`status_message_enabled`, default true) **and** the run has a route (`engine.run_route` → `run.chat_id`). The route is captured once at dispatch from `session.current_route()`; a run with no route never posts (`gateway_status` transport factory returns `None`). One struct per `run_id`. |
| 2 | Host event bus — `engine._progress` → `ctx.emit(...)`, `engine.py:662` `ctx.emit("omo:graph_done", ...)` | No | **Gated (internal)** | `PluginContext.emit` (`hermes_cli/plugins.py:944`) namespaces the event and calls `_manager._dispatch_event`, an in-process subscriber queue. It has no chat transport — subscribers are other plugins/tests. |
| 3 | Background-dispatch acknowledgement — `dispatch(background=True)` return value, via `omo_tools/omo_tool.py` | No | **Gated (not a poster)** | The ack is the tool's **return value** handed back to the calling agent as a tool result; the plugin itself posts nothing. If the calling session's assistant then answers the user, that is the parent session's own turn, not a worker/plugin emission. |
| 4 | Worker subagents (Sisyphus, Hephaestus, Momus, …) | No | **Gated (host)** | Workers launch through `agent/subagent_lifecycle.py:launch` → `_build_child_preserving_parent_tools` → host `_resolve_child_toolsets` (`tools/delegate_tool_toolsets.py`), which strips `DELEGATE_BLOCKED_TOOLS` — `send_message`, `delegate_task`, `clarify`, `memory`, `cronjob_manage` — and the `delegation`/`kanban` toolsets. A child cannot post to chat even though the plugin passes `allowed_toolsets=None` (inherit). The host states it plainly: *"Hermes always blocks unsafe child tools."* (`agent/subagent_lifecycle.py:229`). |
| 5 | `omo` / `omo_task` handlers (`omo_tools/*.py`) | No | **Gated** | They render text to the caller; the only adapter use in the whole plugin is `gateway_status.py`. |
| 6 | Message-id persistence — `StatusNotifier._record` → `engine.note_status_message` | No | n/a | Writes the run record to `state_path` (`orchestrator/store.py`); disk only. |
| 7 | Run registry — `orchestrator/store.py` | No | n/a | Disk only. |

## Findings

- **No leak found.** The one intended surface (path 1) is the only chat writer the
  plugin owns, and it is gated by config **and** by having a dispatch route.
- Path 4 is the interesting one: the plugin does **not** itself apply the host's
  delegation deny-list — it hands the host a bare launch request. Containment rests
  entirely on the host's `_build_child_preserving_parent_tools`. That is the
  documented, host-enforced contract ("Hermes always blocks unsafe child tools"),
  and it holds for the OMO launch path because the plugin goes through
  `subagent_lifecycle.launch`. If a future host build moved the deny-list out of
  `launch` (e.g. into `delegate_task` only), workers would inherit `send_message`
  and become a chat surface. **Worth a regression test on the host side**; it is
  not something the plugin can gate from here.
- Path 1's transport resolves the adapter through the runner's fail-closed
  `_authorization_adapter` lookup rather than the consent-gated `platform_actions`
  facade, because no plugin-facing "send" capability exists to grant (documented in
  the README). The struct is therefore the single deliberate exception to the
  consent facade, and it is the surface the spec asked for.

## What was not checked

- The Discord adapter's own `delete_message` override (the adapter file is not
  present in this host build); the transport calls `adapter.delete_message(chat_id,
  message_id) -> bool`, matching `gateway/platforms/base.py:2435` and
  `gateway/relay/adapter.py:1585`. A platform without it degrades to `False`
  (stale copy retained), never to an error.
- Any host path outside the plugin's call graph.
