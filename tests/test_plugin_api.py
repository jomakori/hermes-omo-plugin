"""Dashboard plugin API: empty/corrupt registry, 404, transcript pagination, 503.

The router must never 500-loop a dashboard that is pointed at a half-written
registry or a host where ``orchestrator`` is not importable. These tests drive
the real router through ``TestClient`` with the registry and live dir redirected
into ``tmp_path``, so ``~/.omo`` is never read.
"""

import json
import sys

import pytest

try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
except ImportError as exc:  # pragma: no cover - depends on the host's test env
    pytest.skip(f"dashboard API tests need fastapi + httpx: {exc}", allow_module_level=True)

from dashboard import plugin_api
from orchestrator import live_transcript, models, store


@pytest.fixture()
def api(tmp_path, monkeypatch):
    """The real router, mounted at its production prefix, on a throwaway state dir."""
    live_dir = tmp_path / "live"
    live_dir.mkdir()
    store_path = tmp_path / "state" / "runs.json"
    monkeypatch.setattr(plugin_api, "_LIVE_DIR", live_dir)
    monkeypatch.setattr(live_transcript, "DEFAULT_LIVE_DIR", str(live_dir))
    monkeypatch.setattr(store, "DEFAULT_STATE_PATH", str(store_path))
    app = FastAPI()
    app.include_router(plugin_api.router, prefix="/api/plugins/omo")
    return {"client": TestClient(app), "live_dir": live_dir, "store_path": store_path}


def _seed_run(store_path, run_id="omo_seed", status=models.RUNNING):
    worker = models.Worker(
        run_id=run_id,
        agent_name="hephaestus",
        task="build the widget",
        chain=("claude",),
        status=status,
        task_id="t1",
        started_at=100.0,
    )
    run = models.Run(run_id=run_id, goal="build the widget", workers=[worker], created_at=200.0)
    store.RunStore(path=store_path).save({run_id: run})
    return run


def _write_transcript(live_dir, run_id="omo_seed", count=5):
    lines = [
        json.dumps(
            {
                "seq": seq,
                "ts": float(seq),
                "agent": "hephaestus",
                "task_id": "t1",
                "kind": "status",
                "text": f"step {seq}",
            }
        )
        for seq in range(1, count + 1)
    ]
    path = live_dir / f"{run_id}.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_missing_store_reads_as_an_empty_list(api):
    assert not api["store_path"].exists()

    response = api["client"].get("/api/plugins/omo/runs")

    assert response.status_code == 200
    assert response.json() == {"runs": []}


def test_corrupt_store_reads_as_an_empty_list(api):
    api["store_path"].parent.mkdir(parents=True)
    api["store_path"].write_text("{ this is not json", encoding="utf-8")

    response = api["client"].get("/api/plugins/omo/runs")

    assert response.status_code == 200
    assert response.json() == {"runs": []}


def test_unknown_run_is_404(api):
    _seed_run(api["store_path"])

    missing = api["client"].get("/api/plugins/omo/runs/omo_absent")
    known = api["client"].get("/api/plugins/omo/runs/omo_seed")

    assert missing.status_code == 404
    assert "not found" in missing.json()["detail"]
    assert known.status_code == 200
    assert known.json()["run_id"] == "omo_seed"


def test_transcript_pagination_advances_next_since(api):
    _seed_run(api["store_path"])
    _write_transcript(api["live_dir"], count=5)

    first = api["client"].get("/api/plugins/omo/runs/omo_seed/transcript", params={"since": 2})
    body = first.json()

    assert first.status_code == 200
    assert [event["seq"] for event in body["events"]] == [3, 4, 5]
    assert body["next_since"] > 2
    assert body["next_since"] == 5
    assert body["active"] is True

    page = api["client"].get("/api/plugins/omo/runs/omo_seed/transcript", params={"since": 0, "limit": 2}).json()

    assert [event["seq"] for event in page["events"]] == [1, 2]
    assert page["next_since"] > 0
    rest = api["client"].get("/api/plugins/omo/runs/omo_seed/transcript", params={"since": page["next_since"]}).json()
    assert [event["seq"] for event in rest["events"]] == [3, 4, 5]


def test_orchestrator_import_failure_is_503(api, monkeypatch):
    # `None` in sys.modules makes the handler's own `from orchestrator.store import
    # RunStore` raise ImportError, which is exactly the bare-harness case.
    monkeypatch.setitem(sys.modules, "orchestrator.store", None)

    response = api["client"].get("/api/plugins/omo/runs")

    assert response.status_code == 503
    assert "orchestrator unavailable" in response.json()["detail"]
