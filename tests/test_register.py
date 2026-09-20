import asyncio
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import __init__ as plugin


class RecordingCtx:
    def __init__(self):
        self.tools = {}
        self.hooks = {}
        self.commands = {}
        self.unload = []
        self.approval_transports = {}
        self.subagent_lifecycle = None

    def register_tool(self, **kwargs):
        self.tools[kwargs["name"]] = kwargs

    def register_hook(self, hook_name, callback):
        self.hooks[hook_name] = callback

    def register_command(self, name, handler, description="", args_hint="", argument_mode=None):
        self.commands[name] = handler

    def register_approval_transport(self, name, present_fn):
        self.approval_transports[name] = present_fn

    def on_unload(self, callback):
        self.unload.append(callback)

    def get_config(self, key, default=None):
        return default


def test_register_wires_tools_hooks_commands_and_transport():
    ctx = RecordingCtx()
    plugin.register(ctx)

    assert set(ctx.tools) == {"omo", "omo_task"}
    for tool in ctx.tools.values():
        assert tool["is_async"] is True
        assert tool["toolset"] == "omo"
        assert set(tool["schema"]) == {"name", "description", "parameters"}
        assert tool["schema"]["parameters"]["type"] == "object"
    assert ctx.tools["omo"]["schema"]["parameters"]["required"] == ["action"]
    assert ctx.tools["omo_task"]["schema"]["parameters"]["required"] == ["prompt"]

    assert set(ctx.hooks) == {"pre_tool_call", "subagent_start", "subagent_stop", "pre_approval_request"}
    assert set(ctx.commands) == {"omo", "omo-approve", "omo-approvals"}
    assert set(ctx.approval_transports) == {"omo"}
    assert len(ctx.unload) == 1


def test_unload_denies_pending_approvals_before_shutdown():
    ctx = RecordingCtx()
    plugin.register(ctx)
    pending = plugin.PendingRegistry(2)
    assert pending.deny_all() == 0
    assert callable(ctx.unload[0])


def test_handlers_accept_positional_args_dict():
    ctx = RecordingCtx()
    plugin.register(ctx)

    omo = ctx.tools["omo"]["handler"]
    out = asyncio.run(omo({"action": "status"}))
    assert "runs" in out

    omo_task = ctx.tools["omo_task"]["handler"]
    out = asyncio.run(omo_task({"prompt": "x", "agent": "explore", "category": "quick"}))
    assert "error" in out

    out = asyncio.run(omo_task({"prompt": ""}))
    assert "error" in out


def test_subagent_start_marks_read_only_and_stop_clears():
    plugin.register(RecordingCtx())
    start = plugin._on_subagent_start
    stop = plugin._on_subagent_stop

    start(metadata={"omo_agent": "oracle"}, session_id="sa-1")
    assert plugin.READ_ONLY_WORKERS.is_read_only("sa-1")

    start(metadata={"omo_agent": "hephaestus"}, session_id="sa-2")
    assert not plugin.READ_ONLY_WORKERS.is_read_only("sa-2")

    stop(session_id="sa-1")
    assert not plugin.READ_ONLY_WORKERS.is_read_only("sa-1")
