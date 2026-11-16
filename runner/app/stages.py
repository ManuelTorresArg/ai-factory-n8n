"""Las etapas de la AI Factory. Cada función = un "job" idempotente que n8n invoca por HTTP.

    intake → spec → build ⇄ (review x4 → approve) → gate → deploy → learn

Regla de diseño: las decisiones críticas tienen un guardarraíl determinista ADEMÁS del LLM
(tests en rojo ⇒ rechazo; paths críticos ⇒ revisión humana). El LLM puede endurecer la decisión,
nunca relajarla.
"""
from __future__ import annotations

import base64
import functools
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from . import gitops
from .agent import run_agent
from .brain import Brain
from .config import load_config, settings
from .llm import LLM, llm_guard
from .loadtest import load_baseline, run_loadtest
from .store import RunStore
from .tools import Workspace, run_cmd

STOPPED = ("failed", "cancelled")                       # nada más puede escribir en el run
FINISHED = ("merged", "failed", "cancelled", "rejected_by_human")
RETRYABLE = ("failed", "cancelled", "rejected_by_human")
ACTIVE = ("created", "specified", "built", "rejected", "approved")  # ocupan un lugar de max_parallel_runs


class RunStopped(RuntimeError):
    """El run fue cancelado o marcado como fallido: la etapa se corta sin escribir nada."""


class BudgetExceeded(RuntimeError):
    """Se pasó el presupuesto de tokens o de tiempo del run."""


def stage(enforce: bool = True):
    """Envuelve una etapa con la guardia del run.

    enforce=True: no arranca si el run está cancelado/fallido o fuera de presupuesto, y la guardia del LLM
    lo vuelve a revisar antes de cada llamada (el Builder frena entre pasos). Si se agota el presupuesto,
    el run queda en failed acá mismo, sin depender de n8n.
    enforce=False (Librarian): sólo cuenta los tokens; corre también sobre runs cerrados.
    """
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(self, run_id: str, *args, **kw):
            guard = RunGuard(self, run_id, enforce)
            token = llm_guard.set(guard)
            try:
                if enforce:
                    guard.check()
                return fn(self, run_id, *args, **kw)
            except BudgetExceeded as exc:
                self.fail(run_id, str(exc))
                raise
            finally:
                llm_guard.reset(token)
        return wrapper
    return deco


class RunGuard:
    def __init__(self, f: "Factory", run_id: str, enforce: bool) -> None:
        self.f, self.run_id, self.enforce = f, run_id, enforce

    def check(self) -> None:
        run = self.f.store.load(self.run_id)
        if run["status"] == "cancelled":
            raise RunStopped("el run fue cancelado")
        if run["status"] == "failed":
            raise RunStopped("el run está marcado como fallido; no se puede continuar")
        b = self.f.p.get("budget", {}) or {}
        used = (run.get("usage") or {}).get("total_tokens", 0)
        if b.get("max_tokens") and used >= int(b["max_tokens"]):
            raise BudgetExceeded(f"presupuesto de tokens agotado: {used} >= {b['max_tokens']}")
        if b.get("max_minutes"):
            mins = (datetime.now(timezone.utc) - datetime.fromisoformat(run["created_at"])).total_seconds() / 60
            if mins > float(b["max_minutes"]):
                raise BudgetExceeded(f"presupuesto de tiempo agotado: {mins:.0f} min > {b['max_minutes']} min")

    def before(self, role: str) -> None:
        if self.enforce:
            self.check()

    def after(self, role: str, usage: dict) -> None:
        p, c = int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)
        if not (p or c):
            return

        def add(r):
            u = r.setdefault("usage", {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "by_role": {}})
            u["prompt_tokens"] += p
            u["completion_tokens"] += c
            u["total_tokens"] += p + c
            u["by_role"][role] = u["by_role"].get(role, 0) + p + c
        self.f.store.update(self.run_id, add)


REVIEWERS = {"security": "reviewer_security", "ux": "reviewer_ux", "ui": "reviewer_ui", "code": "reviewer_code"}
ALL_REVIEWS = [*REVIEWERS, "load"]  # load = Locust, determinista (sin LLM)
TEST_HINTS = ("test", "spec", "__tests__")


class Factory:
    def __init__(self) -> None:
        self.s = settings()
        self.cfg = load_config()
        self.store = RunStore(self.s.runs_dir)
        self.brain = Brain(self.s.brain_dir)
        self.llm = LLM(self.cfg, mock=self.s.mock)
        self.p = self.cfg.get("pipeline", {})
        self.base = self.p.get("base_branch", "main")

    # ---- helpers -------------------------------------------------------
    def prompt(self, name: str) -> str:
        return (self.s.prompts_dir / f"{name}.md").read_text(encoding="utf-8")

    def wt(self, run: dict) -> Path:
        return Path(run["worktree"])

    def _commit(self, run_id: str, fn) -> dict:
        """Escribe el resultado de una etapa sólo si el run sigue vivo. Evita que una etapa que siguió
        corriendo después de un timeout de n8n (o de una cancelación) pise el estado failed/cancelled."""
        def guarded(r):
            if r["status"] in STOPPED:
                raise RunStopped(f"el run quedó en '{r['status']}' mientras corría la etapa; se descarta el resultado")
            fn(r)
        return self.store.update(run_id, guarded)

    def _task_block(self, run: dict) -> str:
        t = run["task"]
        return f"# Tarea ({t['source']}{' ' + str(t['source_ref']) if t.get('source_ref') else ''})\n## {t['title']}\n\n{t['description']}"

    # ---- 1. Intake -----------------------------------------------------
    def active_runs(self) -> list[str]:
        """Runs ocupando un lugar. Uno sin actividad por más que el presupuesto de tiempo se considera muerto."""
        limit_min = float((self.p.get("budget") or {}).get("max_minutes") or 240)
        now = datetime.now(timezone.utc)
        return [r["id"] for r in self.store.list() if r["status"] in ACTIVE
                and (now - datetime.fromisoformat(r["updated_at"])).total_seconds() / 60 <= limit_min]

    def check_capacity(self) -> None:
        mx = int(self.p.get("max_parallel_runs", 0) or 0)
        active = self.active_runs() if mx else []
        if mx and len(active) >= mx:
            raise RuntimeError(f"hay {len(active)} run(s) en curso (max_parallel_runs={mx}): {', '.join(active)}")

    def retry_task(self, run_id: str) -> dict:
        """La tarea de un run cerrado sin merge, lista para volver a entrar por n8n como run nuevo.

        Es un run desde cero (spec incluida), no una reanudación: así no arrastra worktree ni estado
        de una ejecución que falló a mitad de camino."""
        run = self.store.load(run_id)
        if run["status"] not in RETRYABLE:
            raise RuntimeError(f"sólo se reintentan runs {', '.join(RETRYABLE)}; este está en '{run['status']}'")
        busy = [r["id"] for r in self.store.list() if r.get("retry_of") == run_id and r["status"] in ACTIVE]
        if busy:
            raise RuntimeError(f"ya hay un reintento de este run en curso: {busy[0]}")
        self.check_capacity()
        return run["task"] | {"retry_of": run_id}

    def intake(self, task: dict) -> dict:
        gitops.ensure_repo(self.s.workspace, self.base)
        self.check_capacity()
        run = self.store.create(task)
        return {"run_id": run["id"], "status": run["status"]}

    # ---- 2. Spec Writer -----------------------------------------------
    @stage()
    def spec(self, run_id: str) -> dict:
        run = self.store.load(run_id)
        user = (f"{self._task_block(run)}\n\n# Brain\n{self.brain.context()}\n\n"
                f"# Brain: búsqueda por la tarea\n{self.brain.search(run['task']['title'] + ' ' + run['task']['description'])}\n\n"
                f"# Archivos del repo\n{gitops.tree(self.s.workspace)}")
        spec = self.llm.ask_text("spec_writer", self.prompt("spec_writer"), user)
        self.store.write(run_id, "spec.md", spec)
        self._commit(run_id, lambda r: r.update(status="specified"))
        self.store.audit(run_id, "spec", "written", {"model": self.cfg["roles"]["spec_writer"]["model"], "chars": len(spec)})
        return {"run_id": run_id, "status": "specified", "spec_chars": len(spec)}

    # ---- 3. Builder (TDD) ---------------------------------------------
    @stage()
    def build(self, run_id: str) -> dict:
        run = self.store.load(run_id)
        if not run["worktree"]:
            branch = f"{self.p.get('branch_prefix', 'factory/')}{run_id}"
            wt = self.s.worktrees_dir / run_id
            gitops.create_worktree(self.s.workspace, wt, branch, self.base)
            run = self._commit(run_id, lambda r: r.update(branch=branch, worktree=str(wt)))
        iteration = run["iteration"] + 1
        feedback = ""
        if run.get("approval") and run["approval"].get("decision") == "reject":
            feedback = ("# Feedback del Aprobador (iteración anterior) — RESOLVER TODO\n"
                        f"{run['approval'].get('feedback', '')}\n\nDebe corregirse:\n- "
                        + "\n- ".join(run["approval"].get("must_fix", [])))
        ws = Workspace(self.wt(run), self.brain, self.cfg.get("project", {}))
        user = (f"{self._task_block(run)}\n\n# Especificación\n{self.store.read(run_id, 'spec.md')}\n\n"
                f"# Brain (core)\n{self.brain.context(4000)}\n\n{feedback}\n\n"
                f"Iteración {iteration}. Comando de tests: `{ws.test_command}`.")
        rc = self.cfg["roles"]["builder"]
        result = run_agent(self.llm, "builder", self.prompt("builder"), user, ws, int(rc.get("max_steps", 40)))

        # Guardarraíl: los tests los corre la fábrica, no confiamos en lo que reporta el agente.
        t = run_cmd(self.wt(run), ws.test_command)
        tests = {"passed": t["exit_code"] == 0, "exit_code": t["exit_code"], "output": t["output"][-4000:]}
        commit = gitops.commit_all(self.wt(run), f"factory({run_id}): iteración {iteration}\n\n{result['summary'][:500]}")
        files = gitops.changed_files(self.wt(run), self.base)
        tdd = {"tests_touched": any(any(h in f.lower() for h in TEST_HINTS) for f in files)}

        self.store.write(run_id, f"build-{iteration}.json", result | {"tests": tests})

        def upd(r):
            r.update(iteration=iteration, status="built", tests=tests, tdd=tdd, approval=None,
                     build={"finished": result["finished"], "steps": result["steps"], "summary": result["summary"][:500]})
            r["reviews"][str(iteration)] = {}
        self._commit(run_id, upd)
        self.store.audit(run_id, "build", "iteration", {"iteration": iteration, "commit": commit, "steps": result["steps"],
                         "finished": result["finished"], "tests_passed": tests["passed"], "files": files, "usage": result["usage"]})
        return {"run_id": run_id, "iteration": iteration, "commit": commit, "tests_passed": tests["passed"],
                "files_changed": len(files), "agent_finished": result["finished"]}

    # ---- 4. Revisores en paralelo -------------------------------------
    @stage()
    def review(self, run_id: str, kind: str) -> dict:
        if kind not in ALL_REVIEWS:
            raise ValueError(f"revisor desconocido: {kind}")
        run = self.store.load(run_id)
        it = str(run["iteration"])
        if kind == "load":
            verdict = run_loadtest(self.wt(run), self.cfg, self.store.path(run_id), load_baseline(self.baseline_path), tag=it)
            return self._save_review(run_id, it, kind, verdict)
        diff = gitops.diff(self.wt(run), self.base)
        text = (f"{self._task_block(run)}\n\n# Especificación\n{self.store.read(run_id, 'spec.md')}\n\n"
                f"# Tests (ejecutados por la fábrica)\npassed={run['tests']['passed']}\n```\n{run['tests']['output'][-2000:]}\n```\n\n"
                f"# Brain (core)\n{self.brain.context(3000)}\n\n# Diff vs {self.base}\n```diff\n{diff}\n```")
        user: str | list = text
        if kind == "ui":
            shot = screenshot(run["task"].get("ui_url"))
            if shot:
                user = [{"type": "text", "text": text},
                        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{shot}"}}]
        verdict = self.llm.ask_json(REVIEWERS[kind], self.prompt(REVIEWERS[kind]), user, check=review_problem)
        problem = review_problem(verdict)
        if problem:
            # Ni con la corrección respetó el formato: no inventamos un pass, pero tampoco un fail silencioso.
            verdict = {"verdict": "fail", "malformed": True, "findings": [],
                       "summary": f"El revisor no respondió con el formato esperado ({problem}).", "raw": verdict}
        return self._save_review(run_id, it, kind, verdict)

    def _save_review(self, run_id: str, it: str, kind: str, verdict: dict) -> dict:
        verdict.setdefault("verdict", "fail")
        verdict.setdefault("findings", [])
        verdict["reviewer"] = kind
        self.store.write(run_id, f"review-{kind}-{it}.json", verdict)
        self._commit(run_id, lambda r: r["reviews"].setdefault(it, {}).update({kind: verdict}))
        blockers = [f for f in verdict["findings"] if f.get("severity") == "blocker"]
        self.store.audit(run_id, f"review:{kind}", verdict["verdict"], {"iteration": it, "blockers": len(blockers)}
                         | ({"metrics": {k: v for k, v in verdict["metrics"].items() if k != "endpoints"}} if verdict.get("metrics") else {}))
        return {"run_id": run_id, "reviewer": kind, "verdict": verdict["verdict"], "blockers": len(blockers)}

    # ---- 5. Aprobador --------------------------------------------------
    @stage()
    def approve(self, run_id: str) -> dict:
        run = self.store.load(run_id)
        it = str(run["iteration"])
        reviews = run["reviews"].get(it, {})
        missing = [k for k in ALL_REVIEWS if k not in reviews]
        max_it = int(self.p.get("max_iterations", 3))
        if missing:
            raise RuntimeError(f"faltan revisiones: {missing}")
        import json as _j
        user = (f"{self._task_block(run)}\n\n# Especificación\n{self.store.read(run_id, 'spec.md')}\n\n"
                f"# Tests\npassed={run['tests']['passed']}\n# TDD\n{run.get('tdd')}\n\n"
                f"# Revisiones (iteración {it})\n```json\n{_j.dumps(reviews, ensure_ascii=False, indent=1)[:30000]}\n```")
        decision = self.llm.ask_json("approver", self.prompt("approver"), user)
        decision.setdefault("decision", "reject")
        decision.setdefault("must_fix", [])
        # Guardarraíles deterministas (el LLM no puede aprobar esto):
        forced = []
        if not run["tests"]["passed"]:
            forced.append("Los tests no pasan.")
        if not run.get("tdd", {}).get("tests_touched"):
            forced.append("No se agregaron ni modificaron tests (TDD obligatorio).")
        b = run.get("build") or {}
        if b and not b.get("finished"):
            forced.append(f"El Builder agotó max_steps ({b.get('steps')}) sin llamar a finish: el trabajo puede estar incompleto.")
        for k, v in reviews.items():
            blockers = [x.get("issue", "sin detalle") for x in v.get("findings", []) if x.get("severity") == "blocker"]
            if v.get("deterministic") and v.get("verdict") == "fail":
                forced.append(f"{k}: {v.get('summary', 'falló')}")
                forced += blockers
            else:
                # Un blocker de cualquier revisor (también LLM) no lo puede relajar el Aprobador.
                forced += [f"[{k}] {b}" for b in blockers]
        if forced:
            decision["decision"] = "reject"
            decision["must_fix"] = forced + decision["must_fix"]
        approved = decision["decision"] == "approve"
        can_retry = (not approved) and int(it) < max_it
        status = "approved" if approved else ("rejected" if can_retry else "failed")
        self.store.write(run_id, f"approval-{it}.json", decision)
        self._commit(run_id, lambda r: r.update(approval=decision, status=status))
        self.store.audit(run_id, "approve", decision["decision"], {"iteration": it, "forced": forced, "can_retry": can_retry})
        return {"run_id": run_id, "approved": approved, "can_retry": can_retry, "iteration": int(it),
                "max_iterations": max_it, "status": status}

    # ---- 6. Deploy Gate ------------------------------------------------
    @stage()
    def gate(self, run_id: str) -> dict:
        run = self.store.load(run_id)
        d = self.cfg.get("deploy", {})
        files = gitops.changed_files(self.wt(run), self.base)
        n_lines = gitops.lines_changed(self.wt(run), self.base)
        reasons = []
        for f in files:
            pat = gitops.match_any(f, d.get("critical_paths", []))
            if pat:
                reasons.append(f"{f} coincide con path crítico '{pat}'")
        if n_lines > int(d.get("max_auto_lines", 400)):
            reasons.append(f"{n_lines} líneas cambiadas > max_auto_lines")
        llm = self.llm.ask_json("deploy_gate", self.prompt("deploy_gate"),
                                f"{self._task_block(run)}\n\n# Archivos\n" + "\n".join(files) +
                                f"\n\n# Diff\n```diff\n{gitops.diff(self.wt(run), self.base, 30000)}\n```")
        if llm.get("mode") == "human":
            reasons += [f"[agente] {r}" for r in llm.get("reasons", [])] or ["[agente] pidió revisión humana"]
        mode = "human" if reasons else "auto"
        gate = {"mode": mode, "reasons": reasons, "files": files, "lines_changed": n_lines, "agent": llm}
        self.store.write(run_id, "gate.json", gate)
        self._commit(run_id, lambda r: r.update(gate=gate))
        self.store.audit(run_id, "gate", mode, {"reasons": reasons})
        return {"run_id": run_id, "mode": mode, "reasons": reasons}

    # ---- 7. Deploy -----------------------------------------------------
    @stage()
    def deploy(self, run_id: str, force_mode: str | None = None) -> dict:
        run = self.store.load(run_id)
        mode = force_mode or (run.get("gate") or {}).get("mode", "human")
        title = run["task"]["title"]
        dep: dict = {}
        if mode == "auto":
            try:
                sha = gitops.merge_into_base(self.s.workspace, run["branch"], self.base,
                                             f"factory: {title} ({run_id})")
                gitops.remove_worktree(self.s.workspace, self.wt(run))
                dep = {"status": "merged", "commit": sha, "into": self.base}
                self._save_baseline(run, sha)
            except gitops.GitError as exc:
                mode, dep = "human", {"reason": f"merge automático no posible: {exc}"}
        if mode == "human":
            self.store.write(run_id, "PR.md", self._pr_body(run))
            dep.update(status="awaiting_human", branch=run["branch"], pr_file=f"data/runs/{run_id}/PR.md")
        self.store.update(run_id, lambda r: r.update(deploy=dep, status=dep["status"]))
        self.store.audit(run_id, "deploy", dep["status"], dep)
        return {"run_id": run_id, **dep}

    @property
    def baseline_path(self) -> Path:
        return self.s.data_dir / "loadtest-baseline.json"

    def _save_baseline(self, run: dict, sha: str) -> None:
        """Las métricas del último merge son la vara para detectar regresiones del próximo run."""
        lv = run["reviews"].get(str(run["iteration"]), {}).get("load", {})
        if lv.get("metrics"):
            import json as _j
            self.baseline_path.write_text(_j.dumps(lv["metrics"] | {"commit": sha, "run_id": run["id"]}, indent=2))

    @stage()
    def human_decision(self, run_id: str, approve: bool, note: str = "") -> dict:
        run = self.store.load(run_id)
        if run["status"] != "awaiting_human":
            raise RuntimeError(f"el run está en '{run['status']}', no en awaiting_human")
        self.store.audit(run_id, "human", "approve" if approve else "reject", {"note": note})
        if approve:
            return self.deploy(run_id, force_mode="auto")
        self.store.update(run_id, lambda r: r.update(status="rejected_by_human"))
        return {"run_id": run_id, "status": "rejected_by_human"}

    def cancel(self, run_id: str, reason: str = "") -> dict:
        """Stopper manual. La etapa que esté corriendo se corta en su próxima llamada al LLM y no escribe
        su resultado; las siguientes que invoque n8n devuelven 409 y su rama de error termina ahí."""
        def upd(r):
            if r["status"] in FINISHED:
                raise RuntimeError(f"el run ya terminó ('{r['status']}'), no se puede cancelar")
            r.update(status="cancelled")
        self.store.update(run_id, upd)
        self.store.audit(run_id, "pipeline", "cancelled", {"reason": reason or "cancelado a mano"})
        return {"run_id": run_id, "status": "cancelled"}

    def fail(self, run_id: str, reason: str) -> dict:
        if self.store.load(run_id)["status"] == "cancelled":
            return {"run_id": run_id, "status": "cancelled"}  # la cancelación manda sobre el error que provoca
        # Idempotente: el runner ya lo marca ante un error de etapa y después n8n llama a /fail por la rama de error.
        if any(e["stage"] == "pipeline" and e["event"] == "failed" for e in self.store.events(run_id)):
            return {"run_id": run_id, "status": "failed"}
        self.store.update(run_id, lambda r: r.update(status="failed"))
        self.store.audit(run_id, "pipeline", "failed", {"reason": reason})
        return {"run_id": run_id, "status": "failed"}

    # ---- 8. Librarian (actualiza el Brain) -----------------------------
    @stage(enforce=False)
    def learn(self, run_id: str) -> dict:
        run = self.store.load(run_id)
        approvals = [self.store.read(run_id, f"approval-{i}.json") for i in range(1, run["iteration"] + 1)]
        user = (f"{self._task_block(run)}\n\nEstado final: {run['status']} tras {run['iteration']} iteración(es).\n\n"
                f"# Especificación\n{self.store.read(run_id, 'spec.md')[:6000]}\n\n"
                f"# Decisiones del aprobador por iteración\n" + "\n".join(approvals)[:12000] +
                f"\n\n# Brain actual\n{self.brain.context(4000)}")
        out = self.llm.ask_json("librarian", self.prompt("librarian"), user, check=librarian_problem)
        problem = librarian_problem(out)
        if problem:
            # No escribimos basura en el Brain, pero tampoco lo registramos como actualizado.
            self.store.audit(run_id, "learn", "malformed", {"problem": problem, "raw": out})
            return {"run_id": run_id, "lessons": 0, "decisions": 0, "status": run["status"], "malformed": True}
        lessons, decisions = out.get("lessons", []), out.get("decisions", [])
        self.brain.append("LESSONS.md", run["task"]["title"], lessons, run_id)
        self.brain.append("DECISIONS.md", run["task"]["title"], decisions, run_id)
        self.store.audit(run_id, "learn", "brain_updated", {"lessons": lessons, "decisions": decisions})
        return {"run_id": run_id, "lessons": len(lessons), "decisions": len(decisions),
                "status": run["status"]}

    def _pr_body(self, run: dict) -> str:
        it = str(run["iteration"])
        rv = run["reviews"].get(it, {})
        lines = [f"# {run['task']['title']}", "", f"Rama: `{run['branch']}` → `{self.base}`  ·  run `{run['id']}`", "",
                 "## Por qué requiere revisión humana"] + [f"- {r}" for r in (run.get("gate") or {}).get("reasons", [])] + [
                 "", "## Revisores"] + [f"- **{k}**: {v.get('verdict')} — {v.get('summary', '')}" for k, v in rv.items()] + [
                 "", "## Aprobador", (run.get("approval") or {}).get("feedback", ""), "",
                 "## Spec", self.store.read(run["id"], "spec.md"), "",
                 "Aprobar: `POST /runs/<id>/human-decision {\"approve\": true}`"]
        return "\n".join(lines)


def review_problem(v: dict) -> str | None:
    """Valida la forma de prompts/_review_format.md. None = OK."""
    if v.get("verdict") not in ("pass", "fail"):
        return f"falta \"verdict\" con valor \"pass\" o \"fail\" (recibí las claves {sorted(v)})"
    if not isinstance(v.get("findings", []), list):
        return "\"findings\" tiene que ser una lista"
    return None


def librarian_problem(v: dict) -> str | None:
    """Valida {"lessons": [str], "decisions": [str]} (prompts/librarian.md). None = OK.

    Una clave ausente cuenta como lista vacía (los modelos suelen omitir `decisions` cuando no hay);
    se rechaza sólo si no viene ninguna de las dos o si alguna viene con otro tipo."""
    if "lessons" not in v and "decisions" not in v:
        return f"faltan \"lessons\" y \"decisions\" (recibí las claves {sorted(v)})"
    for key in ("lessons", "decisions"):
        val = v.get(key, [])
        if not isinstance(val, list) or not all(isinstance(x, str) for x in val):
            return f"\"{key}\" tiene que ser una lista de textos"
    return None


def screenshot(url: str | None) -> str | None:
    """Captura opcional para el revisor de UI (requiere build con WITH_PLAYWRIGHT=true)."""
    if not url:
        return None
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None
    try:
        with sync_playwright() as pw:
            b = pw.chromium.launch()
            page = b.new_page(viewport={"width": 1366, "height": 900})
            page.goto(url, wait_until="networkidle", timeout=30000)
            png = page.screenshot(full_page=True)
            b.close()
        return base64.b64encode(png).decode()
    except Exception:
        return None
