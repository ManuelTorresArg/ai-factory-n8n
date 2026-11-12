"""Prueba de carga con Locust: el 5º revisor, el único 100% determinista (sin LLM).

Flujo dentro del worktree del run:
  1. (opcional) levanta la app con `start_command` en un puerto libre ({port})
  2. espera a que `health_path` responda
  3. corre `locust --headless` con el locustfile del repo
  4. compara el agregado contra umbrales fijos y contra la baseline del último merge

Analogía: es el readinessProbe + un smoke de carga antes de dejar pasar el rollout.
El veredicto tiene la misma forma JSON que los revisores LLM, así el Aprobador lo trata igual.
"""
from __future__ import annotations

import csv
import json
import os
import shlex
import signal
import socket
import subprocess
import time
from pathlib import Path

import httpx

from .tools import run_cmd


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _skip(reason: str) -> dict:
    return {"verdict": "pass", "score": 10, "summary": f"Carga: no aplica ({reason})", "findings": [],
            "skipped": True, "deterministic": True}


def _wait_ready(url: str, timeout: float, proc: subprocess.Popen | None) -> str | None:
    deadline = time.time() + timeout
    last = "sin respuesta"
    while time.time() < deadline:
        if proc and proc.poll() is not None:
            return f"la app terminó al arrancar (exit {proc.returncode})"
        try:
            r = httpx.get(url, timeout=2)
            if r.status_code < 500:
                return None
            last = f"HTTP {r.status_code}"
        except httpx.HTTPError as exc:
            last = type(exc).__name__
        time.sleep(0.5)
    return f"no respondió en {timeout:.0f}s ({last})"


def parse_stats(csv_path: Path) -> dict:
    with open(csv_path, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    agg = next((r for r in rows if r.get("Name") == "Aggregated"), None)
    if not agg or int(agg["Request Count"] or 0) == 0:
        raise ValueError("Locust no registró requests")
    reqs = int(agg["Request Count"])
    endpoints = [{"name": f"{r['Type']} {r['Name']}", "requests": int(r["Request Count"]),
                  "failures": int(r["Failure Count"]), "p95_ms": float(r["95%"] or 0)}
                 for r in rows if r.get("Name") != "Aggregated"]
    return {"requests": reqs, "failures": int(agg["Failure Count"]),
            "failure_ratio": int(agg["Failure Count"]) / reqs,
            "median_ms": float(agg["Median Response Time"] or 0), "p95_ms": float(agg["95%"] or 0),
            "p99_ms": float(agg["99%"] or 0), "rps": float(agg["Requests/s"] or 0), "endpoints": endpoints}


def evaluate(m: dict, th: dict, baseline: dict | None, max_regression_pct: float) -> list[dict]:
    f = []
    if m["p95_ms"] > float(th.get("p95_ms", 500)):
        f.append({"severity": "blocker", "file": "-", "issue": f"p95 {m['p95_ms']:.0f} ms > umbral {th['p95_ms']} ms",
                  "suggestion": "Perfilar el endpoint más lento (ver endpoints) antes de reintentar."})
    if m["failure_ratio"] > float(th.get("failure_ratio", 0.01)):
        f.append({"severity": "blocker", "file": "-",
                  "issue": f"{m['failure_ratio']:.1%} de requests fallidos > umbral {float(th['failure_ratio']):.1%}",
                  "suggestion": "Revisar errores 4xx/5xx bajo concurrencia (locks, pool de conexiones, timeouts)."})
    if m["rps"] < float(th.get("min_rps", 0)):
        f.append({"severity": "blocker", "file": "-", "issue": f"{m['rps']:.1f} req/s < mínimo {th['min_rps']}",
                  "suggestion": "Buscar operaciones bloqueantes en el camino del request."})
    if baseline and baseline.get("p95_ms"):
        limit = baseline["p95_ms"] * (1 + max_regression_pct / 100)
        if m["p95_ms"] > limit:
            f.append({"severity": "blocker", "file": "-",
                      "issue": f"Regresión: p95 {m['p95_ms']:.0f} ms vs baseline {baseline['p95_ms']:.0f} ms "
                               f"(+{(m['p95_ms'] / baseline['p95_ms'] - 1):.0%}, máx. +{max_regression_pct:.0f}%)",
                      "suggestion": f"Comparar con el merge {baseline.get('commit', '?')}; el cambio degradó la latencia."})
    for e in m["endpoints"]:
        if e["p95_ms"] > float(th.get("p95_ms", 500)):
            f.append({"severity": "major", "file": "-", "issue": f"{e['name']}: p95 {e['p95_ms']:.0f} ms",
                      "suggestion": "Endpoint por encima del umbral global."})
    return f


def run_loadtest(wt: Path, cfg: dict, out_dir: Path, baseline: dict | None, tag: str = "1") -> dict:
    lt = cfg.get("loadtest", {})
    if not lt.get("enabled", False):
        return _skip("deshabilitada en factory.yaml")
    locustfile = wt / lt.get("locustfile", "loadtest/locustfile.py")
    if not locustfile.exists():
        return _skip(f"el repo no tiene {lt.get('locustfile', 'loadtest/locustfile.py')}")

    port = free_port()
    target = lt.get("target_url", "http://127.0.0.1:{port}").format(port=port)
    proc = None
    if lt.get("start_command"):
        cmd = shlex.split(lt["start_command"].format(port=port))
        log = open(out_dir / f"loadtest-app-{tag}.log", "w", encoding="utf-8")
        proc = subprocess.Popen(cmd, cwd=wt, stdout=log, stderr=subprocess.STDOUT,
                                start_new_session=True, env=os.environ | {"PORT": str(port)})
    try:
        err = _wait_ready(target.rstrip("/") + lt.get("health_path", "/"), float(lt.get("startup_timeout_s", 30)), proc)
        if err:
            return {"verdict": "fail", "score": 0, "summary": f"Carga: la app no levantó — {err}", "deterministic": True,
                    "findings": [{"severity": "blocker", "file": lt.get("start_command", "-"), "issue": err,
                                  "suggestion": f"Ver data/runs/<id>/loadtest-app-{tag}.log"}]}
        prefix = out_dir / f"locust-{tag}"
        r = run_cmd(wt, f"locust -f {shlex.quote(str(locustfile))} --headless -u {int(lt.get('users', 20))} "
                        f"-r {int(lt.get('spawn_rate', 5))} -t {lt.get('run_time', '30s')} --host {target} "
                        f"--csv {shlex.quote(str(prefix))} --only-summary",
                    timeout=int(lt.get("timeout_s", 300)))
        try:
            m = parse_stats(Path(f"{prefix}_stats.csv"))
        except (FileNotFoundError, ValueError, KeyError) as exc:
            return {"verdict": "fail", "score": 0, "summary": f"Carga: Locust no produjo métricas ({exc})",
                    "deterministic": True, "findings": [{"severity": "blocker", "file": str(locustfile.relative_to(wt)),
                                                         "issue": r["output"][-800:], "suggestion": "Revisar el locustfile."}]}
    finally:
        if proc and proc.poll() is None:
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)

    findings = evaluate(m, lt.get("thresholds", {}), baseline, float(lt.get("max_regression_pct", 30)))
    blockers = any(x["severity"] == "blocker" for x in findings)
    base_txt = f" · baseline p95 {baseline['p95_ms']:.0f} ms" if baseline else " · sin baseline"
    return {"verdict": "fail" if blockers else "pass", "score": 3 if blockers else 10, "deterministic": True,
            "summary": f"Carga: {m['requests']} req, p95 {m['p95_ms']:.0f} ms, {m['rps']:.0f} req/s, "
                       f"errores {m['failure_ratio']:.1%}{base_txt}",
            "metrics": m, "findings": findings}


def load_baseline(path: Path) -> dict | None:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
