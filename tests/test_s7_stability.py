"""S7 服务稳定性测试。"""

import importlib
from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from app.background import reset_executor


def test_lifespan_starts_and_stops_background_workers(tmp_path, monkeypatch):
    db_path = tmp_path / "ready.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("ARTIFACTS_DIR", str(tmp_path / "artifacts"))

    import app.config as cfg
    import app.db as db_mod
    import app.repository as repo_mod
    import app.background as bg_mod
    import app.main as main_mod

    cfg.get_settings.cache_clear()
    importlib.reload(cfg)
    importlib.reload(db_mod)
    importlib.reload(repo_mod)
    importlib.reload(bg_mod)
    importlib.reload(main_mod)
    reset_executor()

    with TestClient(main_mod.app) as client:
        assert bg_mod.is_accepting_jobs() is True
        assert client.get("/health").json() == {"status": "ok"}
        ready = client.get("/ready")
        assert ready.status_code == 200
        assert ready.json() == {"status": "ready"}
        assert ready.headers.get("x-request-id")

    assert bg_mod.is_accepting_jobs() is False
    reset_executor()
    cfg.get_settings.cache_clear()


def test_ready_rejects_when_workers_stopped(tmp_path, monkeypatch):
    db_path = tmp_path / "ready-stopped.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("ARTIFACTS_DIR", str(tmp_path / "artifacts"))

    import app.config as cfg
    import app.db as db_mod
    import app.repository as repo_mod
    import app.background as bg_mod
    import app.main as main_mod

    cfg.get_settings.cache_clear()
    importlib.reload(cfg)
    importlib.reload(db_mod)
    importlib.reload(repo_mod)
    importlib.reload(bg_mod)
    importlib.reload(main_mod)
    reset_executor()
    with TestClient(main_mod.app) as client:
        bg_mod.stop_background_workers()
        response = client.get("/ready")
        assert response.status_code == 503
        assert response.json()["detail"]["code"] == "WORKER_NOT_READY"
    cfg.get_settings.cache_clear()
