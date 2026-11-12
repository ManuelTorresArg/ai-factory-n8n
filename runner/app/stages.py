"""Las etapas de la AI Factory. Cada función = un "job" idempotente que n8n invoca por HTTP.

    intake → spec → build ⇄ (review x4 → approve) → gate → deploy → learn

Regla de diseño: las decisiones críticas tienen un guardarraíl determinista ADEMÁS del LLM
(tests en rojo ⇒ rechazo; paths críticos ⇒ revisión humana). El LLM puede endurecer la decisión,
nunca relajarla.
"""
from __future__ import annotations

import base64
from pathlib import Path

from . import gitops
from .agent import run_agent
from .brain import Brain
from .config import load_config, settings
from .llm import LLM
from .loadtest import load_baseline, run_loadtest
from .store import RunStore
from .tools import Workspace, run_cmd

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

    def _task_block(self, run: dict) -> str:
        t = run["task"]
        return f"# Tarea ({t['source']}{' ' + str(t['source_ref']) if t.get('source_ref') else ''})\n## {t['title']}\n\n{t['description']}"

    # ---- 1. Intake -----------------------------------------------------
    def intake(self, task: dict) -> dict:
        gitops.ensure_repo(self.s.workspace, self.base)
        run = self.store.create(task)
        return {"run_id": run["id"], "status": run["status"]}

    # ---- 2. Spec Writer -----------------------------------------------
    def spec(self, run_id: str) -> dict:
        run = self.store.load(run_id)
        user = (f"{self._task_block(run)}\n\n# Brain\n{self.brain.context()}\n\n"
                f"# Brain: búsqueda por la tarea\n{self.brain.search(run['task']['title'] + ' ' + run['task']['description'])}\n\n"
                f"# Archivos del repo\n{gitops.tree(self.s.workspace)}")
        spec = self.llm.ask_text("spec_writer", self.prompt("spec_writer"), user)
        self.store.write(run_id, "spec.md", spec)
        self.store.update(run_id, lambda r: r.update(status="specified"))
        self.store.audit(run_id, "spec", "written", {"model": self.cfg["roles"]["spec_writer"]["model"], "chars": len(spec)})
        return {"run_id": run_id, "status": "specified", "spec_chars": len(spec)}

    # ---- 3. Builder (TDD) ---------------------------------------------
    def build(self, run_id: str) -> dict:
        run = self.store.load(run_id)
        if not run["worktree"]:
            branch = f"{self.p.get('branch_prefix', 'factory/')}{run_id}"
            wt = self.s.worktrees_dir / run_id
            gitops.create_worktree(self.s.workspace, wt, branch, self.base)
            run = self.store.update(run_id, lambda r: r.update(branch=branch, worktree=str(wt)))
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
            r.update(iteration=iteration, status="built", tests=tests, tdd=tdd, approval=None)
            r["reviews"][str(iteration)] = {}
        self.store.update(run_id, upd)
        self.store.audit(run_id, "build", "iteration", {"iteration": iteration, "commit": commit, "steps": result["steps"],
                         "finished": result["finished"], "tests_passed": tests["passed"], "files": files, "usage": result["usage"]})
        return {"run_id": run_id, "iteration": iteration, "commit": commit, "tests_passed": tests["passed"],
                "files_changed": len(files), "agent_finished": result["finished"]}

    # ---- 4. Revisores en paralelo -------------------------------------
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
        verdict = self.llm.ask_json(REVIEWERS[kind], self.prompt(REVIEWERS[kind]), user)
        return self._save_review(run_id, it, kind, verdict)

    def _save_review(self, run_id: str, it: str, kind: str, verdict: dict) -> dict:
        verdict.setdefault("verdict", "fail")
        verdict.setdefault("findings", [])
        verdict["reviewer"] = kind
        self.store.write(run_id, f"review-{kind}-{it}.json", verdict)
        self.store.update(run_id, lambda r: r["reviews"].setdefault(it, {}).update({kind: verdict}))
        blockers = [f for f in verdict["findings"] if f.get("severity") == "blocker"]
        self.store.audit(run_id, f"review:{kind}", verdict["verdict"], {"iteration": it, "blockers": len(blockers)}
                         | ({"metrics": {k: v for k, v in verdict["metrics"].items() if k != "endpoints"}} if verdict.get("metrics") else {}))
        return {"run_id": run_id, "reviewer": kind, "verdict": verdict["verdict"], "blockers": len(blockers)}

    # ---- 5. Aprobador --------------------------------------------------
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
        for k, v in reviews.items():
            if v.get("deterministic") and v.get("verdict") == "fail":
                forced.append(f"{k}: {v.get('summary', 'falló')}")
                forced += [x["issue"] for x in v.get("findings", []) if x.get("severity") == "blocker"]
        if forced:
            decision["decision"] = "reject"
            decision["must_fix"] = forced + decision["must_fix"]
        approved = decision["decision"] == "approve"
        can_retry = (not approved) and int(it) < max_it
        status = "approved" if approved else ("rejected" if can_retry else "failed")
        self.store.write(run_id, f"approval-{it}.json", decision)
        self.store.update(run_id, lambda r: r.update(approval=decision, status=status))
        self.store.audit(run_id, "approve", decision["decision"], {"iteration": it, "forced": forced, "can_retry": can_retry})
        return {"run_id": run_id, "approved": approved, "can_retry": can_retry, "iteration": int(it),
                "max_iterations": max_it, "status": status}

    # ---- 6. Deploy Gate ------------------------------------------------
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
        self.store.update(run_id, lambda r: r.update(gate=gate))
        self.store.audit(run_id, "gate", mode, {"reasons": reasons})
        return {"run_id": run_id, "mode": mode, "reasons": reasons}

    # ---- 7. Deploy -----------------------------------------------------
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

    def human_decision(self, run_id: str, approve: bool, note: str = "") -> dict:
        run = self.store.load(run_id)
        if run["status"] != "awaiting_human":
            raise RuntimeError(f"el run está en '{run['status']}', no en awaiting_human")
        self.store.audit(run_id, "human", "approve" if approve else "reject", {"note": note})
        if approve:
            return self.deploy(run_id, force_mode="auto")
        self.store.update(run_id, lambda r: r.update(status="rejected_by_human"))
        return {"run_id": run_id, "status": "rejected_by_human"}

    def fail(self, run_id: str, reason: str) -> dict:
        self.store.update(run_id, lambda r: r.update(status="failed"))
        self.store.audit(run_id, "pipeline", "failed", {"reason": reason})
        return {"run_id": run_id, "status": "failed"}

    # ---- 8. Librarian (actualiza el Brain) -----------------------------
    def learn(self, run_id: str) -> dict:
        run = self.store.load(run_id)
        approvals = [self.store.read(run_id, f"approval-{i}.json") for i in range(1, run["iteration"] + 1)]
        user = (f"{self._task_block(run)}\n\nEstado final: {run['status']} tras {run['iteration']} iteración(es).\n\n"
                f"# Especificación\n{self.store.read(run_id, 'spec.md')[:6000]}\n\n"
                f"# Decisiones del aprobador por iteración\n" + "\n".join(approvals)[:12000] +
                f"\n\n# Brain actual\n{self.brain.context(4000)}")
        out = self.llm.ask_json("librarian", self.prompt("librarian"), user)
        self.brain.append("LESSONS.md", run["task"]["title"], out.get("lessons", []), run_id)
        self.brain.append("DECISIONS.md", run["task"]["title"], out.get("decisions", []), run_id)
        self.store.audit(run_id, "learn", "brain_updated", out)
        return {"run_id": run_id, "lessons": len(out.get("lessons", [])), "decisions": len(out.get("decisions", [])),
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
