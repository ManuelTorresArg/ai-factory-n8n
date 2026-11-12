"""Prueba de punta a punta con LLM mock sobre un repo git temporal, vía la API HTTP (como n8n)."""
import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]


def sh(cwd, *cmd):
    subprocess.run(cmd, cwd=cwd, check=True, capture_output=True)


LOCUSTFILE = """from locust import HttpUser, task, constant


class U(HttpUser):
    wait_time = constant(0.05)

    @task
    def index(self):
        self.client.get("/")
"""


def make_client(tmp_path, monkeypatch, loadtest=None):
    repo = tmp_path / "repo"
    repo.mkdir()
    sh(repo, "git", "init", "-b", "main")
    (repo / "README.md").write_text("demo\n")
    if loadtest is not None:
        (repo / "loadtest").mkdir()
        (repo / "loadtest" / "locustfile.py").write_text(LOCUSTFILE)
    sh(repo, "git", "add", ".")
    sh(repo, "git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "init")
    brain = tmp_path / "brain"
    brain.mkdir()
    for f in (ROOT / "brain").glob("*.md"):
        (brain / f.name).write_text(f.read_text())
    cfg_path = ROOT / "factory.yaml"
    if loadtest is not None:
        cfg = yaml.safe_load(cfg_path.read_text())
        cfg["loadtest"].update(loadtest)
        cfg_path = tmp_path / "factory.yaml"
        cfg_path.write_text(yaml.safe_dump(cfg))
    env = {"WORKSPACE": str(repo), "DATA_DIR": str(tmp_path / "data"), "BRAIN_DIR": str(brain),
           "PROMPTS_DIR": str(ROOT / "prompts"), "FACTORY_CONFIG": str(cfg_path),
           "FACTORY_MOCK": "1", "FACTORY_TOKEN": "t0k", "GIT_AUTHOR_NAME": "AI Factory",
           "GIT_AUTHOR_EMAIL": "factory@local", "GIT_COMMITTER_NAME": "AI Factory", "GIT_COMMITTER_EMAIL": "factory@local"}
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    from app import config, main
    config.settings.cache_clear()
    main.factory.cache_clear()
    from fastapi.testclient import TestClient
    c = TestClient(main.app, headers={"X-Factory-Token": "t0k"})
    c.repo, c.brain, c.data = repo, brain, tmp_path / "data"
    return c


@pytest.fixture
def client(tmp_path, monkeypatch):
    return make_client(tmp_path, monkeypatch)


def pipeline(c, title="Saludo"):
    rid = c.post("/runs", json={"title": title, "description": "función greet"}).json()["run_id"]
    assert c.post(f"/runs/{rid}/spec").status_code == 200
    while True:
        b = c.post(f"/runs/{rid}/build").json()
        assert b["tests_passed"], b
        for k in ("security", "ux", "ui", "code"):
            assert c.post(f"/runs/{rid}/review/{k}").json()["verdict"] == "pass"
        c.load = c.post(f"/runs/{rid}/review/load").json()
        a = c.post(f"/runs/{rid}/approve").json()
        if a["approved"] or not a["can_retry"]:
            break
    return rid, a


def test_auth(client):
    assert client.post("/runs", json={"title": "x"}, headers={"X-Factory-Token": "mal"}).status_code == 401


def test_happy_path_auto_merge(client):
    rid, a = pipeline(client)
    assert a["approved"] and a["iteration"] == 1
    assert client.post(f"/runs/{rid}/gate").json()["mode"] == "auto"
    d = client.post(f"/runs/{rid}/deploy").json()
    assert d["status"] == "merged", d
    assert (client.repo / "greet.py").exists()  # quedó mergeado en main del repo local
    assert client.post(f"/runs/{rid}/learn").json()["lessons"] == 1
    assert "Los helpers de saludo" in (client.brain / "LESSONS.md").read_text()
    stages = [e["stage"] for e in client.get(f"/runs/{rid}/audit").json()]
    assert stages[0] == "intake" and "review:ui" in stages and stages[-1] == "learn"


def test_reject_loop_then_approve(client, monkeypatch):
    monkeypatch.setenv("FACTORY_MOCK_REJECT_FIRST", "1")
    rid, a = pipeline(client)
    assert a["approved"] and a["iteration"] == 2


def test_human_gate_and_decision(client, monkeypatch):
    monkeypatch.setenv("FACTORY_MOCK_GATE", "human")
    rid, _ = pipeline(client)
    g = client.post(f"/runs/{rid}/gate").json()
    assert g["mode"] == "human"
    d = client.post(f"/runs/{rid}/deploy").json()
    assert d["status"] == "awaiting_human" and not (client.repo / "greet.py").exists()
    assert "revisión humana" in (Path(os.environ["DATA_DIR"]) / "runs" / rid / "PR.md").read_text()
    h = client.post(f"/runs/{rid}/human-decision", json={"approve": True}).json()
    assert h["status"] == "merged" and (client.repo / "greet.py").exists()


def test_critical_path_forces_human(client):
    # Brain/CONVENTIONS permite tests; tocar un .sql es crítico por regla determinista aunque el agente diga auto
    rid, _ = pipeline(client)
    wt = Path(client.get(f"/runs/{rid}").json()["worktree"])
    (wt / "db").mkdir()
    (wt / "db" / "001.sql").write_text("create table x(id int);\n")
    sh(wt, "git", "add", ".")
    sh(wt, "git", "commit", "-m", "sql")
    g = client.post(f"/runs/{rid}/gate").json()
    assert g["mode"] == "human" and any("db/001.sql" in r for r in g["reasons"])


def test_path_escape_blocked(tmp_path):
    from app.brain import Brain
    from app.tools import Workspace
    ws = Workspace(tmp_path, Brain(tmp_path / "b"), {"allowed_commands": ["pytest"]})
    assert "ERROR" in ws.call("read_file", {"path": "../../etc/passwd"})
    assert "no permitido" in ws.call("run_command", {"command": "rm -rf /"})


# ---- Prueba de carga (Locust) --------------------------------------------
HTTP_APP = {"start_command": "python -m http.server {port} --bind 127.0.0.1", "users": 5, "spawn_rate": 5,
            "run_time": "3s", "startup_timeout_s": 15}


def test_load_skipped_without_locustfile(client):
    rid, a = pipeline(client)
    assert client.load["verdict"] == "pass" and a["approved"]
    review = client.get(f"/runs/{rid}").json()["reviews"]["1"]["load"]
    assert review["skipped"] and "no aplica" in review["summary"]


def test_load_runs_and_sets_baseline(tmp_path, monkeypatch):
    c = make_client(tmp_path, monkeypatch, loadtest=HTTP_APP)
    rid, a = pipeline(c)
    m = c.get(f"/runs/{rid}").json()["reviews"]["1"]["load"]["metrics"]
    assert a["approved"] and m["requests"] > 10 and m["failure_ratio"] == 0
    assert c.post(f"/runs/{rid}/gate").json()["mode"] == "auto"
    assert c.post(f"/runs/{rid}/deploy").json()["status"] == "merged"
    base = json.loads((c.data / "loadtest-baseline.json").read_text())
    assert base["run_id"] == rid and base["p95_ms"] == m["p95_ms"]
    assert (c.data / "runs" / rid / "locust-1_stats.csv").exists()


def test_load_threshold_forces_reject(tmp_path, monkeypatch):
    c = make_client(tmp_path, monkeypatch, loadtest=HTTP_APP | {"thresholds": {"p95_ms": 0, "failure_ratio": 0.01}})
    rid, a = pipeline(c)
    assert c.load["verdict"] == "fail" and c.load["blockers"] >= 1
    # el mock del Aprobador dice "approve", pero el guardarraíl determinista lo pisa
    assert not a["approved"] and a["iteration"] == 3 and not a["can_retry"]
    must_fix = c.get(f"/runs/{rid}").json()["approval"]["must_fix"]
    assert any("p95" in x for x in must_fix)


def test_load_app_that_never_starts(tmp_path, monkeypatch):
    c = make_client(tmp_path, monkeypatch, loadtest=HTTP_APP | {"start_command": "python -c 'import sys; sys.exit(3)'"})
    rid, a = pipeline(c)
    assert c.load["verdict"] == "fail" and not a["approved"]


def test_regression_vs_baseline():
    from app.loadtest import evaluate
    m = {"p95_ms": 130, "failure_ratio": 0, "rps": 50, "endpoints": []}
    th = {"p95_ms": 500, "failure_ratio": 0.01}
    assert evaluate(m, th, {"p95_ms": 120}, 30) == []
    f = evaluate(m, th, {"p95_ms": 80, "commit": "abc123"}, 30)
    assert f[0]["severity"] == "blocker" and "Regresión" in f[0]["issue"] and "abc123" in f[0]["suggestion"]
