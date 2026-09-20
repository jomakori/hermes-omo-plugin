import threading
import time

from approvals.policy import (
    AUTO_ACK,
    DEFAULT_ALLOW_CHOICES,
    Mode,
    allow_choices,
    max_pending,
    permitted_choices,
    resolve_mode,
)
from approvals.registry import PendingRegistry
from approvals.transport import SessionKeys, make_present_fn


class FakeDecision:
    def __init__(self, request_id, digest, choice):
        self.request_id = request_id
        self.digest = digest
        self.choice = choice


class FakeRequest:
    def __init__(self, *, timeout_seconds=300.0, allowed_choices=("once", "session", "always", "deny")):
        self.request_id = "a" * 32
        self.digest = "d" * 64
        self.description = "rm -rf /"
        self.pattern_key = "rm_rf"
        self.surface = "transport:omo"
        self.timeout_seconds = timeout_seconds
        self.allowed_choices = allowed_choices

    def respond(self, choice):
        return FakeDecision(self.request_id, self.digest, choice)


class FakeCtx:
    def __init__(self, config=None, inject_ok=True):
        self._config = config or {}
        self._inject_ok = inject_ok
        self.injected = []

    def get_config(self, key, default=None):
        return self._config.get(key, default)

    def inject_message(self, content, role="user", *, session_key=None):
        self.injected.append((content, session_key))
        return self._inject_ok


def engine_ctx(**config):
    return FakeCtx(config)


def test_mode_defaults_to_forward():
    assert resolve_mode(FakeCtx()) is Mode.FORWARD


def test_auto_requires_the_acknowledgement():
    assert resolve_mode(FakeCtx({"approvals.mode": "auto"})) is Mode.FORWARD
    assert resolve_mode(FakeCtx({"approvals.mode": "auto", "approvals.auto_ack": "nope"})) is Mode.FORWARD
    assert resolve_mode(FakeCtx({"approvals.mode": "auto", "approvals.auto_ack": AUTO_ACK})) is Mode.AUTO


def test_unknown_mode_falls_back_to_forward_not_auto():
    assert resolve_mode(FakeCtx({"approvals.mode": "banana"})) is Mode.FORWARD


def test_deny_mode():
    assert resolve_mode(FakeCtx({"approvals.mode": "deny"})) is Mode.DENY


def test_allow_choices_default_and_intersection():
    assert allow_choices(FakeCtx()) == DEFAULT_ALLOW_CHOICES
    request = FakeRequest(allowed_choices=("once", "session", "deny"))
    assert permitted_choices(request, ("once", "deny")) == ("once", "deny")
    assert permitted_choices(request, ("always",)) == ("deny",)


def test_max_pending_is_clamped_below_the_host_pool():
    assert max_pending(FakeCtx()) == 6
    assert max_pending(FakeCtx({"approvals.max_pending": 99})) == 7
    assert max_pending(FakeCtx({"approvals.max_pending": 0})) == 1


def test_registry_capacity_resolve_and_deny_all():
    registry = PendingRegistry(1)
    first = registry.add(FakeRequest(), choices=("once", "deny"), session_key="s1", timeout_seconds=30)
    assert first is not None
    assert registry.add(FakeRequest(), choices=("once", "deny"), session_key="s1", timeout_seconds=30) is None

    ok, _ = registry.resolve(first.short_id, "session")
    assert ok is False
    ok, _ = registry.resolve(first.short_id, "once")
    assert ok is True
    ok, _ = registry.resolve(first.short_id, "deny")
    assert ok is False

    assert registry.deny_all() == 0


def test_registry_never_defaults_to_a_non_deny_choice():
    registry = PendingRegistry(2)
    request = FakeRequest(timeout_seconds=0.0)
    pending = registry.add(request, choices=("once", "deny"), session_key="s", timeout_seconds=0.0)
    expired = registry.expire()
    assert pending in expired
    assert pending.decision == "deny"


def test_deny_mode_returns_deny():
    present = make_present_fn(FakeCtx({"approvals.mode": "deny"}), PendingRegistry(2))
    assert present(FakeRequest()).choice == "deny"


def test_auto_mode_returns_once():
    ctx = FakeCtx({"approvals.mode": "auto", "approvals.auto_ack": AUTO_ACK})
    present = make_present_fn(ctx, PendingRegistry(2))
    assert present(FakeRequest()).choice == "once"
    assert ctx.injected == []


def test_forward_without_session_key_fails_closed():
    present = make_present_fn(FakeCtx(), PendingRegistry(2))
    assert present(FakeRequest()).choice == "deny"


def test_forward_denies_when_injection_is_refused():
    ctx = FakeCtx(inject_ok=False)
    registry = PendingRegistry(2)
    present = make_present_fn(ctx, registry)
    from approvals.transport import _KEYS

    _KEYS.record("a" * 32, "session-1")
    assert present(FakeRequest()).choice == "deny"
    assert registry.snapshot() == []


def test_forward_returns_the_users_choice():
    ctx = FakeCtx()
    registry = PendingRegistry(2)
    present = make_present_fn(ctx, registry)
    from approvals.transport import _KEYS

    request = FakeRequest(timeout_seconds=5.0)
    _KEYS.record(request.request_id, "session-1")

    result = {}

    def run():
        result["decision"] = present(request)

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    deadline = time.time() + 3
    pending = None
    while time.time() < deadline:
        pending = registry.snapshot()
        if pending:
            break
        time.sleep(0.01)
    assert pending, "transport never registered a pending approval"
    assert ctx.injected and ctx.injected[0][1] == "session-1"
    assert registry.resolve(pending[0]["id"], "once")[0] is True
    worker.join(timeout=3)
    assert result["decision"].choice == "once"


def test_forward_times_out_to_deny():
    ctx = FakeCtx()
    registry = PendingRegistry(2)
    present = make_present_fn(ctx, registry)
    from approvals.transport import _KEYS

    request = FakeRequest(timeout_seconds=0.3)
    _KEYS.record(request.request_id, "session-1")
    started = time.time()
    assert present(request).choice == "deny"
    assert time.time() - started < 3


def test_unexpected_exception_denies():
    class Boom(FakeCtx):
        def get_config(self, key, default=None):
            raise RuntimeError("config exploded")

    present = make_present_fn(Boom(), PendingRegistry(2))
    assert present(FakeRequest()).choice == "deny"


def test_session_keys_are_single_use():
    keys = SessionKeys()
    keys.record("req-1", "session-1")
    assert keys.take("req-1") == "session-1"
    assert keys.take("req-1") is None
