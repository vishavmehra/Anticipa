"""ANTICIPA_EMBEDDED_WORKER wiring: off by default; on => the job-claim loop runs in a background thread for
the
lifetime of the app and is stopped on shutdown. The loop itself is faked; no database."""

from __future__ import annotations

import threading

from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from anticipa.config import Settings
from anticipa.jobs import worker as worker_module
from anticipa.main import create_app


def _app(**overrides):
    settings = Settings(_env_file=None, env="local", supabase_url="https://x.supabase.co", **overrides)
    return create_app(settings, engine=create_engine("postgresql+psycopg://n:n@127.0.0.1:1/n"))


def test_flag_defaults_off_and_reads_the_env_var(monkeypatch):
    assert Settings(_env_file=None).embedded_worker is False
    monkeypatch.setenv("ANTICIPA_EMBEDDED_WORKER", "1")
    assert Settings(_env_file=None).embedded_worker is True


def test_flag_off_starts_no_worker(monkeypatch):
    started = []
    monkeypatch.setattr(worker_module, "run_worker", lambda *a, **k: started.append(a))
    app = _app()
    with TestClient(app):
        assert app.state.embedded_worker is None
    assert started == []


def test_flag_on_runs_one_worker_thread_and_stops_it_on_shutdown(monkeypatch):
    seen: dict = {}
    running = threading.Event()

    def fake_run_worker(sessions, cfg, stop, **kwargs):
        seen.update(sessions=sessions, cfg=cfg, stop=stop, kwargs=kwargs, thread=threading.current_thread())
        running.set()
        stop.wait(10)  # a real loop polls until `stop` is set

    monkeypatch.setattr(worker_module, "run_worker", fake_run_worker)
    app = _app(embedded_worker=True)
    with TestClient(app):
        assert running.wait(5)
        assert app.state.embedded_worker.alive
        assert seen["sessions"] is app.state.sessionmaker  # the API's own sessionmaker: no second engine
        assert seen["cfg"].poll_interval == app.state.settings.job_poll_interval_seconds
        assert seen["kwargs"].get("exit_when_idle", False) is False
        assert seen["thread"].name == "embedded-worker" and seen["thread"].daemon
        assert not seen["stop"].is_set()
    assert seen["stop"].is_set()  # shutdown asked the loop to stop...
    assert not app.state.embedded_worker.alive  # ...and waited for it


def test_a_crashing_loop_does_not_take_the_api_down(monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("boom")

    monkeypatch.setattr(worker_module, "run_worker", boom)
    app = _app(embedded_worker=True)
    with TestClient(app) as client:
        assert client.get("/api/v1/health").status_code == 200
