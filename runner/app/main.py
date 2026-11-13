"""API HTTP del factory-runner. n8n llama un endpoint por etapa.

    POST /runs                         intake  → {run_id}
    POST /runs/{id}/spec               Spec Writer
    POST /runs/{id}/build              Builder (TDD, worktree propio)
    POST /runs/{id}/review/{kind}      security | ux | ui | code   (n8n los llama en paralelo)
    POST /runs/{id}/approve            Aprobador → {approved, can_retry}
    POST /runs/{id}/gate               Deploy Gate → {mode: auto|human}
    POST /runs/{id}/deploy             merge local o PR.md para humano
    POST /runs/{id}/human-decision     {approve: bool, note}
    POST /runs/{id}/fail               marca el run como fallido
    POST /runs/{id}/learn              Librarian actualiza el Brain
    GET  /runs, /runs/{id}, /runs/{id}/audit, /health

Para la UI (GET /ui):
    GET  /pipeline                     componentes y configuración (sin secretos)
    GET  /runs/{id}/files[/{name}]     artefactos del run
    GET  /brain[/{name}], /prompts/{name}
    POST /submit                       reenvía la tarea al webhook de n8n
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse, RedirectResponse
from pydantic import BaseModel

from .config import settings
from .stages import Factory

app = FastAPI(title="AI Factory runner", version="0.1.0")


@lru_cache
def factory() -> Factory:
    return Factory()


def auth(x_factory_token: str = Header(default="")) -> None:
    token = settings().token
    if token and x_factory_token != token:
        raise HTTPException(401, "X-Factory-Token inválido")


class Task(BaseModel):
    title: str
    description: str = ""
    source: str = "manual"
    source_ref: str | None = None
    ui_url: str | None = None


class HumanDecision(BaseModel):
    approve: bool
    note: str = ""


class Fail(BaseModel):
    reason: str = "sin detalle"


def _run(fn, *args, **kw):
    try:
        return fn(*args, **kw)
    except FileNotFoundError:
        raise HTTPException(404, "run inexistente")
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(409, str(exc))


@app.get("/health")
def health():
    f = factory()
    return {"ok": True, "mock": f.s.mock, "workspace": str(f.s.workspace), "omniroute": f.llm.base_url}


@app.post("/runs", dependencies=[Depends(auth)])
def create(task: Task):
    return _run(factory().intake, task.model_dump())


@app.get("/runs", dependencies=[Depends(auth)])
def runs():
    return factory().store.list()


@app.get("/runs/{run_id}", dependencies=[Depends(auth)])
def get_run(run_id: str):
    return _run(factory().store.load, run_id)


@app.get("/runs/{run_id}/audit", dependencies=[Depends(auth)])
def audit(run_id: str):
    raw = _run(factory().store.read, run_id, "audit.jsonl")
    return [json.loads(l) for l in raw.splitlines() if l]


@app.post("/runs/{run_id}/spec", dependencies=[Depends(auth)])
def spec(run_id: str):
    return _run(factory().spec, run_id)


@app.post("/runs/{run_id}/build", dependencies=[Depends(auth)])
def build(run_id: str):
    return _run(factory().build, run_id)


@app.post("/runs/{run_id}/review/{kind}", dependencies=[Depends(auth)])
def review(run_id: str, kind: str):
    return _run(factory().review, run_id, kind)


@app.post("/runs/{run_id}/approve", dependencies=[Depends(auth)])
def approve(run_id: str):
    return _run(factory().approve, run_id)


@app.post("/runs/{run_id}/gate", dependencies=[Depends(auth)])
def gate(run_id: str):
    return _run(factory().gate, run_id)


@app.post("/runs/{run_id}/deploy", dependencies=[Depends(auth)])
def deploy(run_id: str):
    return _run(factory().deploy, run_id)


@app.post("/runs/{run_id}/human-decision", dependencies=[Depends(auth)])
def human(run_id: str, body: HumanDecision):
    return _run(factory().human_decision, run_id, body.approve, body.note)


@app.post("/runs/{run_id}/fail", dependencies=[Depends(auth)])
def fail(run_id: str, body: Fail | None = None):
    return _run(factory().fail, run_id, (body or Fail()).reason)


@app.post("/runs/{run_id}/learn", dependencies=[Depends(auth)])
def learn(run_id: str):
    return _run(factory().learn, run_id)


# ---- UI -------------------------------------------------------------------
UI_DIR = Path(__file__).parent / "ui"
_SAFE_NAME = re.compile(r"^[\w.-]+$")


def _safe_file(root: Path, name: str, suffix: str | None = None) -> Path:
    """Sólo nombres planos dentro de root: nada de ../ ni subcarpetas."""
    if not _SAFE_NAME.match(name) or name.startswith(".") or (suffix and not name.endswith(suffix)):
        raise HTTPException(400, "nombre de archivo inválido")
    f = root / name
    if not f.is_file():
        raise HTTPException(404, "archivo inexistente")
    return f


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse("/ui")


@app.get("/ui", include_in_schema=False)
def ui():
    return FileResponse(UI_DIR / "index.html")


@app.get("/pipeline", dependencies=[Depends(auth)])
def pipeline():
    f = factory()
    cfg = f.cfg
    return {
        "mock": f.s.mock,
        "omniroute": cfg.get("omniroute", {}).get("base_url"),
        "n8n": f.s.n8n_public_url,
        "roles": cfg.get("roles", {}),
        "pipeline": cfg.get("pipeline", {}),
        "project": {"test_command": cfg.get("project", {}).get("test_command"),
                    "allowed_commands": cfg.get("project", {}).get("allowed_commands", [])},
        "loadtest": cfg.get("loadtest", {}),
        "deploy": cfg.get("deploy", {}),
        "prompts": sorted(p.stem for p in f.s.prompts_dir.glob("*.md")),
    }


@app.get("/runs/{run_id}/files", dependencies=[Depends(auth)])
def run_files(run_id: str):
    return _run(factory().store.files, run_id)


@app.get("/runs/{run_id}/files/{name}", dependencies=[Depends(auth)], response_class=PlainTextResponse)
def run_file(run_id: str, name: str):
    root = _run(factory().store.path, run_id)
    return _safe_file(root, name).read_text(encoding="utf-8", errors="replace")


@app.get("/brain", dependencies=[Depends(auth)])
def brain():
    root = factory().s.brain_dir
    return [{"name": p.name, "size": p.stat().st_size} for p in sorted(root.glob("*.md"))]


@app.get("/brain/{name}", dependencies=[Depends(auth)], response_class=PlainTextResponse)
def brain_file(name: str):
    return _safe_file(factory().s.brain_dir, name, ".md").read_text(encoding="utf-8")


@app.get("/prompts/{name}", dependencies=[Depends(auth)], response_class=PlainTextResponse)
def prompt_file(name: str):
    return _safe_file(factory().s.prompts_dir, f"{name}.md").read_text(encoding="utf-8")


@app.post("/submit", dependencies=[Depends(auth)])
def submit(task: Task):
    """Igual que scripts/submit.sh: la tarea entra por n8n, que orquesta todas las etapas."""
    try:
        r = httpx.post(settings().n8n_webhook, json=task.model_dump(), timeout=15)
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"n8n no responde en {settings().n8n_webhook}: {exc}")
    if r.status_code >= 400:
        raise HTTPException(502, f"n8n respondió {r.status_code}: {r.text[:300]}")
    return {"ok": True, "n8n": r.text[:300]}
