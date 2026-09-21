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
        self.skills = {}
        self.unload = []
        self.subagent_lifecycle = None

    def register_tool(self, **kwargs):
        self.tools[kwargs["name"]] = kwargs

    def register_hook(self, hook_name, callback):
        self.hooks[hook_name] = callback

    def register_command(self, name, handler, description=""):
        self.commands[name] = handler

    def register_skill(self, name, path, description=""):
        self.skills[name] = path

    def on_unload(self, callback):
        self.unload.append(callback)

    def get_config(self, key, default=None):
        return default


def test_register_wires_tools_and_command():
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

    assert ctx.hooks == {}
    assert set(ctx.commands) == {"omo"}
    assert set(ctx.skills) == set(plugin.AGENTS)
    assert len(ctx.unload) == 1


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


def test_handlers_and_command_return_strings():
    ctx = RecordingCtx()
    plugin.register(ctx)

    for name, tool in ctx.tools.items():
        out = asyncio.run(tool["handler"]({"action": "status", "prompt": "x"}))
        assert isinstance(out, str), f"{name} handler returned {type(out).__name__}, not str"

    out = ctx.commands["omo"]("")
    assert isinstance(out, str), f"/omo returned {type(out).__name__}, not str"


class ConfigCtx(RecordingCtx):
    def __init__(self, config):
        super().__init__()
        self._config = config

    def get_config(self, key, default=None):
        return self._config.get(key, default)


def test_settings_carries_the_fallback_and_category_layers():
    ctx = ConfigCtx(
        {
            "runtime_fallback": {"enabled": True, "retry_on_errors": [402]},
            "categories": {"quick": ["litellm/x"]},
            "chains": {"explore": ["litellm/y"]},
        }
    )
    settings = plugin._settings(ctx)
    assert settings["runtime_fallback"] == {"enabled": True, "retry_on_errors": [402]}
    assert settings["categories"] == {"quick": ["litellm/x"]}
    assert settings["chains"] == {"explore": ["litellm/y"]}


def test_persona_is_delivered_through_the_launch_context():
    from orchestrator.personas import compose_context

    composed = compose_context("explore", None)
    assert composed.startswith('<persona agent="explore" binding="authoritative">')
    assert composed.rstrip().endswith("</persona>")
    assert "You are a codebase search specialist." in composed


def test_persona_context_keeps_caller_context_and_never_exceeds_the_host_cap():
    from orchestrator.personas import MAX_CONTEXT_CHARS, compose_context

    composed = compose_context("sisyphus", "caller notes")
    assert composed is not None
    assert len(composed) <= MAX_CONTEXT_CHARS
    assert "caller notes" in composed


def test_oversized_persona_is_truncated_with_a_pointer_to_the_full_skill():
    from orchestrator.personas import MAX_CONTEXT_CHARS, compose_context, persona_text

    assert len(persona_text("sisyphus")) > MAX_CONTEXT_CHARS
    composed = compose_context("sisyphus", None)
    assert 'skill_view("omo:sisyphus")' in composed
    assert len(composed) <= MAX_CONTEXT_CHARS


def test_agent_without_a_persona_still_carries_caller_context():
    from orchestrator.personas import compose_context

    assert compose_context("nope", None) is None
    assert compose_context("nope", "ctx") == "<task_context>\nctx\n</task_context>"
