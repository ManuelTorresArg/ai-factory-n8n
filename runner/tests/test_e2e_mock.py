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


def make_client(tmp_path, monkeypatch, loadtest=None, patch=None):
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
    if loadtest is not None or patch:
        cfg = yaml.safe_load(cfg_path.read_text())
        cfg["loadtest"].update(loadtest or {})
        if patch:
            patch(cfg)
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


def test_ui_endpoints(client):
    rid, _ = pipeline(client)
    assert client.get("/ui").status_code == 200
    p = client.get("/pipeline").json()
    assert "builder" in p["roles"] and "api_key" not in str(p) and "builder" in p["prompts"]
    names = [f["name"] for f in client.get(f"/runs/{rid}/files").json()]
    assert {"run.json", "spec.md", "approval-1.json"} <= set(names)
    assert "Spec" in client.get(f"/runs/{rid}/files/spec.md").text
    assert client.get(f"/runs/{rid}/files/..%2Frun.json").status_code in (400, 404)
    assert client.get(f"/runs/{rid}/files/nope.md").status_code == 404
    assert client.get("/prompts/builder").status_code == 200
    assert client.get("/prompts/..%2F..%2Fetc%2Fpasswd").status_code in (400, 404)
    assert any(f["name"] == "LESSONS.md" for f in client.get("/brain").json())
    assert client.get("/pipeline", headers={"X-Factory-Token": "mal"}).status_code == 401


def test_malformed_review_is_retried(client, monkeypatch):
    monkeypatch.setenv("FACTORY_MOCK_BAD_REVIEW", "once")
    rid, a = pipeline(client)  # pipeline() exige verdict == pass en los 4 revisores LLM
    assert a["approved"]


def test_malformed_review_is_flagged(client, monkeypatch):
    monkeypatch.setenv("FACTORY_MOCK_BAD_REVIEW", "always")
    rid = client.post("/runs", json={"title": "x", "description": "y"}).json()["run_id"]
    client.post(f"/runs/{rid}/spec")
    client.post(f"/runs/{rid}/build")
    r = client.post(f"/runs/{rid}/review/code").json()
    assert r["verdict"] == "fail"
    v = client.get(f"/runs/{rid}").json()["reviews"]["1"]["code"]
    assert v["malformed"] and "formato" in v["summary"] and v["raw"]["outcome"] == "passed"


# ---- Robustez ante errores de ejecución ------------------------------------
def run_until_reviews(c, kinds=("security", "ux", "ui", "code", "load")):
    rid = c.post("/runs", json={"title": "x", "description": "y"}).json()["run_id"]
    assert c.post(f"/runs/{rid}/spec").status_code == 200
    assert c.post(f"/runs/{rid}/build").json()["tests_passed"]
    for k in kinds:
        c.post(f"/runs/{rid}/review/{k}")
    return rid


def test_llm_down_marks_run_failed(client, monkeypatch):
    monkeypatch.setenv("FACTORY_MOCK_LLM_DOWN", "spec_writer")
    rid = client.post("/runs", json={"title": "x", "description": "y"}).json()["run_id"]
    r = client.post(f"/runs/{rid}/spec")
    assert r.status_code == 502 and "no respondió" in r.json()["detail"]
    assert client.get(f"/runs/{rid}").json()["status"] == "failed"
    # n8n vuelve a avisar por la rama de error: no se duplica en la bitácora
    client.post(f"/runs/{rid}/fail", json={"reason": "Spec Writer: 502"})
    fails = [e for e in client.get(f"/runs/{rid}/audit").json() if e["stage"] == "pipeline"]
    assert len(fails) == 1 and "spec" in fails[0]["data"]["reason"]


def test_failed_reviewer_blocks_merge(client, monkeypatch):
    monkeypatch.setenv("FACTORY_MOCK_LLM_DOWN", "reviewer_code")
    rid = run_until_reviews(client)
    assert client.get(f"/runs/{rid}").json()["status"] == "failed"
    for stage in ("approve", "gate", "deploy"):
        assert client.post(f"/runs/{rid}/{stage}").status_code == 409, stage
    assert not (client.repo / "greet.py").exists()


def test_llm_blocker_forces_reject(client, monkeypatch):
    monkeypatch.setenv("FACTORY_MOCK_BLOCKER", "reviewer_ui")
    rid = run_until_reviews(client)
    a = client.post(f"/runs/{rid}/approve").json()  # el mock del Aprobador dice "approve"
    assert not a["approved"] and a["can_retry"]
    must_fix = client.get(f"/runs/{rid}").json()["approval"]["must_fix"]
    assert any(x.startswith("[ui]") and "no respeta la spec" in x for x in must_fix)


def test_bytecode_is_not_committed(client):
    rid, _ = pipeline(client)
    built = next(e for e in client.get(f"/runs/{rid}/audit").json() if e["stage"] == "build")["data"]["files"]
    assert built and not any("__pycache__" in f or f.endswith(".pyc") for f in built), built
    assert client.post(f"/runs/{rid}/gate").json()["mode"] == "auto"
    assert client.post(f"/runs/{rid}/deploy").json()["status"] == "merged"
    tracked = subprocess.run(["git", "ls-files"], cwd=client.repo, capture_output=True, text=True).stdout
    assert "greet.py" in tracked and ".pyc" not in tracked


def test_malformed_librarian_does_not_touch_brain(client, monkeypatch):
    monkeypatch.setenv("FACTORY_MOCK_BAD_LIBRARIAN", "1")
    rid, _ = pipeline(client)
    before = (client.brain / "LESSONS.md").read_text()
    out = client.post(f"/runs/{rid}/learn").json()
    assert out["malformed"] and out["lessons"] == 0
    assert (client.brain / "LESSONS.md").read_text() == before
    last = client.get(f"/runs/{rid}/audit").json()[-1]
    assert last["stage"] == "learn" and last["event"] == "malformed" and "lessons" in last["data"]["problem"]


def test_librarian_without_decisions_still_saves_lessons(client, monkeypatch):
    monkeypatch.setenv("FACTORY_MOCK_BAD_LIBRARIAN", "partial")
    rid, _ = pipeline(client)
    out = client.post(f"/runs/{rid}/learn").json()
    assert out["lessons"] == 1 and out["decisions"] == 0 and not out.get("malformed")
    assert "Copiar la plantilla existente." in (client.brain / "LESSONS.md").read_text()
    last = client.get(f"/runs/{rid}/audit").json()[-1]
    assert last["event"] == "brain_updated" and last["data"] == {"lessons": ["Copiar la plantilla existente."], "decisions": []}


# ---- Stoppers ----------------------------------------------------------------
def test_cancel_stops_pipeline(client):
    rid = client.post("/runs", json={"title": "x", "description": "y"}).json()["run_id"]
    assert client.post(f"/runs/{rid}/spec").status_code == 200
    assert client.post(f"/runs/{rid}/cancel", json={"reason": "me equivoqué de tarea"}).json()["status"] == "cancelled"
    r = client.post(f"/runs/{rid}/build")
    assert r.status_code == 409 and "cancelado" in r.json()["detail"]
    client.post(f"/runs/{rid}/fail", json={"reason": "Builder: 409"})  # rama de error de n8n
    assert client.get(f"/runs/{rid}").json()["status"] == "cancelled"
    assert client.post(f"/runs/{rid}/cancel").status_code == 409  # ya terminó
    ev = [e for e in client.get(f"/runs/{rid}/audit").json() if e["stage"] == "pipeline"]
    assert [e["event"] for e in ev] == ["cancelled"] and "equivoqué" in ev[0]["data"]["reason"]


def test_stage_running_when_cancelled_is_cut_and_discarded(client):
    from app import main
    from app.stages import RunStopped
    rid = client.post("/runs", json={"title": "x", "description": "y"}).json()["run_id"]
    f = main.factory()
    calls = []
    real = f.llm._chat

    def chat(role, rc, messages, tools):  # cancelan el run mientras el Builder está a mitad de camino
        calls.append(role)
        if len(calls) == 2:
            f.cancel(rid, "desde la UI")
        return real(role, rc, messages, tools)
    f.llm._chat = chat
    try:
        f.spec(rid)
        with pytest.raises(RunStopped):
            f.build(rid)
    finally:
        f.llm._chat = real
    run = f.store.load(rid)
    assert run["status"] == "cancelled" and run["iteration"] == 0 and len(calls) == 2


def test_late_result_does_not_overwrite_failed(client):
    from app import main
    from app.stages import RunStopped
    rid = client.post("/runs", json={"title": "x", "description": "y"}).json()["run_id"]
    f = main.factory()
    f.spec(rid)
    real = f.llm._chat

    def chat(role, rc, messages, tools):  # n8n cortó por timeout y marcó failed; el modelo contesta después
        out = real(role, rc, messages, tools)
        f.fail(rid, "Spec Writer: timeout de n8n")
        return out
    f.llm._chat = chat
    try:
        with pytest.raises(RunStopped):
            f.spec(rid)
    finally:
        f.llm._chat = real
    assert f.store.load(rid)["status"] == "failed"


def test_token_budget_fails_run(tmp_path, monkeypatch):
    c = make_client(tmp_path, monkeypatch, patch=lambda cfg: cfg["pipeline"].update(budget={"max_tokens": 1500}))
    monkeypatch.setenv("FACTORY_MOCK_USAGE", "1000")
    rid = c.post("/runs", json={"title": "x", "description": "y"}).json()["run_id"]
    assert c.post(f"/runs/{rid}/spec").status_code == 200
    r = c.post(f"/runs/{rid}/build")  # 1.ª llamada: 1000 < 1500 pasa; 2.ª: 2000 ≥ 1500 corta
    assert r.status_code == 409 and "presupuesto de tokens" in r.json()["detail"]
    run = c.get(f"/runs/{rid}").json()
    assert run["status"] == "failed" and run["usage"]["total_tokens"] == 2000
    assert run["usage"]["by_role"] == {"spec_writer": 1000, "builder": 1000}
    fails = [e for e in c.get(f"/runs/{rid}/audit").json() if e["event"] == "failed"]
    assert len(fails) == 1 and "presupuesto" in fails[0]["data"]["reason"]


def test_time_budget_fails_run(tmp_path, monkeypatch):
    c = make_client(tmp_path, monkeypatch, patch=lambda cfg: cfg["pipeline"].update(budget={"max_minutes": 0.0001}))
    rid = c.post("/runs", json={"title": "x", "description": "y"}).json()["run_id"]
    import time
    time.sleep(0.05)
    r = c.post(f"/runs/{rid}/spec")
    assert r.status_code == 409 and "tiempo" in r.json()["detail"]
    assert c.get(f"/runs/{rid}").json()["status"] == "failed"


def test_builder_without_finish_forces_reject(tmp_path, monkeypatch):
    c = make_client(tmp_path, monkeypatch, patch=lambda cfg: cfg["roles"]["builder"].update(max_steps=2))
    rid = run_until_reviews(c)  # el mock escribe y corre tests, pero no llega a llamar a finish
    a = c.post(f"/runs/{rid}/approve").json()
    assert not a["approved"]
    assert any("max_steps" in x for x in c.get(f"/runs/{rid}").json()["approval"]["must_fix"])


def test_max_parallel_runs(tmp_path, monkeypatch):
    c = make_client(tmp_path, monkeypatch, patch=lambda cfg: cfg["pipeline"].update(max_parallel_runs=1))
    rid = c.post("/runs", json={"title": "a"}).json()["run_id"]
    r = c.post("/runs", json={"title": "b"})
    assert r.status_code == 409 and rid in r.json()["detail"]
    assert c.post("/submit", json={"title": "b"}).status_code == 409  # se avisa antes de llamar a n8n
    c.post(f"/runs/{rid}/cancel")
    assert c.post("/runs", json={"title": "b"}).status_code == 200


def test_llm_4xx_is_not_retried(monkeypatch):
    import httpx
    from app.llm import LLM, LLMError
    calls = []

    def post(self, url, **kw):
        calls.append(url)
        return httpx.Response(400, text="modelo inexistente", request=httpx.Request("POST", url))
    monkeypatch.setattr(httpx.Client, "post", post)
    monkeypatch.setattr("app.llm.time.sleep", lambda s: None)
    llm = LLM({"omniroute": {"base_url": "http://x/v1", "retries": 2}, "roles": {"r": {"model": "m"}}})
    with pytest.raises(LLMError, match="400"):
        llm.chat("r", [{"role": "user", "content": "hola"}])
    assert len(calls) == 1


# ---- Reintento -----------------------------------------------------------------
def fake_n8n(monkeypatch, c):
    """Simula el webhook de n8n: lo que recibe lo manda a POST /runs, como el nodo Create Run."""
    sent = []

    def post(url, json=None, timeout=None):
        sent.append(json)
        c.created = c.post("/runs", json=json).json()["run_id"]
        return __import__("httpx").Response(200, text='{"message":"Workflow was started"}')
    monkeypatch.setattr("app.main.httpx.post", post)
    return sent


def test_retry_failed_run(client, monkeypatch):
    monkeypatch.setenv("FACTORY_MOCK_LLM_DOWN", "spec_writer")
    rid = client.post("/runs", json={"title": "Mayonesa", "description": "receta casera"}).json()["run_id"]
    assert client.post(f"/runs/{rid}/spec").status_code == 502
    monkeypatch.delenv("FACTORY_MOCK_LLM_DOWN")  # volvió OmniRoute
    sent = fake_n8n(monkeypatch, client)
    r = client.post(f"/runs/{rid}/retry")
    assert r.status_code == 200 and r.json()["retry_of"] == rid
    assert sent[0]["title"] == "Mayonesa" and sent[0]["retry_of"] == rid
    new = client.get(f"/runs/{client.created}").json()
    assert new["retry_of"] == rid and new["task"]["description"] == "receta casera"
    assert client.post(f"/runs/{client.created}/spec").status_code == 200
    # mientras el reintento está activo no se puede pedir otro
    r2 = client.post(f"/runs/{rid}/retry")
    assert r2.status_code == 409 and client.created in r2.json()["detail"]
    assert client.get(f"/runs/{rid}/audit").json()[-1]["event"] == "retry_requested"
    assert any(x["retry_of"] == rid for x in client.get("/runs").json())


def test_retry_cancelled_run_and_not_merged(client, monkeypatch):
    fake_n8n(monkeypatch, client)
    rid = client.post("/runs", json={"title": "x"}).json()["run_id"]
    assert client.post(f"/runs/{rid}/retry").status_code == 409  # sigue activo
    client.post(f"/runs/{rid}/cancel")
    assert client.post(f"/runs/{rid}/retry").status_code == 200
    mid, _ = pipeline(client, "otro")
    client.post(f"/runs/{mid}/gate"); client.post(f"/runs/{mid}/deploy")
    r = client.post(f"/runs/{mid}/retry")
    assert r.status_code == 409 and "merged" in r.json()["detail"]
