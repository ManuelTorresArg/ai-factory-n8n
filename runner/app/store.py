"""Estado de cada run en disco: data/runs/<run_id>/

  run.json      estado actual (lo que lee n8n)
  audit.jsonl   bitácora append-only de cada evento (quién, qué etapa, qué modelo, resultado)
  *.md / *.json artefactos por etapa e iteración (spec.md, review-security-1.json, ...)

Es el equivalente a los logs de jobs de un pipeline de GitLab: cada run es reproducible y auditable.
"""
from __future__ import annotations

import json
import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path

_locks: dict[str, threading.Lock] = {}
_global = threading.Lock()


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _lock(run_id: str) -> threading.Lock:
    with _global:
        return _locks.setdefault(run_id, threading.Lock())


class RunStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    # ---- ciclo de vida -------------------------------------------------
    def create(self, task: dict) -> dict:
        run_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)
        run = {
            "id": run_id,
            "status": "created",
            "created_at": now(),
            "updated_at": now(),
            "task": {
                "title": task.get("title") or "Sin título",
                "description": task.get("description", ""),
                "source": task.get("source", "manual"),  # manual | sentry | github | gitlab | mantis
                "source_ref": task.get("source_ref"),
                "ui_url": task.get("ui_url"),
            },
            "iteration": 0,
            "branch": None,
            "worktree": None,
            "tests": None,
            "reviews": {},
            "approval": None,
            "gate": None,
            "deploy": None,
            "retry_of": task.get("retry_of"),  # run fallido/cancelado del que este es reintento
        }
        (self.root / run_id).mkdir(parents=True)
        self.save(run)
        self.audit(run_id, "intake", "created", run["task"] | ({"retry_of": run["retry_of"]} if run["retry_of"] else {}))
        return run

    def path(self, run_id: str) -> Path:
        p = (self.root / run_id).resolve()
        if p.parent != self.root.resolve() or not p.exists():
            raise FileNotFoundError(run_id)
        return p

    def load(self, run_id: str) -> dict:
        return json.loads((self.path(run_id) / "run.json").read_text(encoding="utf-8"))

    def save(self, run: dict) -> None:
        run["updated_at"] = now()
        target = self.root / run["id"] / "run.json"
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps(run, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(target)

    def update(self, run_id: str, fn) -> dict:
        """Read-modify-write con lock: los 4 revisores escriben en paralelo."""
        with _lock(run_id):
            run = self.load(run_id)
            fn(run)
            self.save(run)
            return run

    def list(self) -> list[dict]:
        out = []
        for d in sorted(self.root.iterdir(), reverse=True):
            f = d / "run.json"
            if f.exists():
                r = json.loads(f.read_text(encoding="utf-8"))
                out.append({k: r.get(k) for k in ("id", "status", "iteration", "branch", "created_at", "updated_at", "retry_of")}
                           | {"title": r["task"]["title"], "source": r["task"].get("source")})
        return out

    # ---- artefactos y auditoría ----------------------------------------
    def write(self, run_id: str, name: str, content: str | dict) -> None:
        if not isinstance(content, str):
            content = json.dumps(content, indent=2, ensure_ascii=False)
        (self.path(run_id) / name).write_text(content, encoding="utf-8")

    def read(self, run_id: str, name: str) -> str:
        f = self.path(run_id) / name
        return f.read_text(encoding="utf-8") if f.exists() else ""

    def files(self, run_id: str) -> list[dict]:
        """Artefactos del run (spec.md, review-*.json, PR.md, CSV de Locust...) para la UI."""
        return [{"name": f.name, "size": f.stat().st_size}
                for f in sorted(self.path(run_id).iterdir()) if f.is_file() and not f.name.endswith(".tmp")]

    def events(self, run_id: str) -> list[dict]:
        return [json.loads(l) for l in self.read(run_id, "audit.jsonl").splitlines() if l.strip()]

    def audit(self, run_id: str, stage: str, event: str, data: dict | None = None) -> None:
        line = {"ts": now(), "stage": stage, "event": event, "data": data or {}}
        with open(self.path(run_id) / "audit.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(line, ensure_ascii=False) + "\n")
