"""Herramientas que el Builder puede usar dentro de SU worktree (y nada fuera de él).

Guardas:
  - rutas resueltas y confinadas al worktree; .git es intocable
  - comandos sólo por allowlist (prefijos en factory.yaml > project.allowed_commands), sin shell
  - timeouts y salida truncada
"""
from __future__ import annotations

import shlex
import subprocess
from pathlib import Path

from .brain import Brain

MAX_OUT = 8000


def _tail(text: str, n: int = MAX_OUT) -> str:
    return text if len(text) <= n else "[...]\n" + text[-n:]


def run_cmd(cwd: Path, cmd: str, timeout: int = 600) -> dict:
    try:
        p = subprocess.run(shlex.split(cmd), cwd=cwd, capture_output=True, text=True, timeout=timeout)
        return {"command": cmd, "exit_code": p.returncode, "output": _tail(p.stdout + p.stderr)}
    except FileNotFoundError as exc:
        return {"command": cmd, "exit_code": 127, "output": str(exc)}
    except subprocess.TimeoutExpired:
        return {"command": cmd, "exit_code": 124, "output": f"timeout {timeout}s"}


class Workspace:
    def __init__(self, root: Path, brain: Brain, project_cfg: dict) -> None:
        self.root = root.resolve()
        self.brain = brain
        self.test_command = project_cfg.get("test_command", "pytest -q")
        self.allowed = project_cfg.get("allowed_commands", [])
        self.log: list[dict] = []  # trazabilidad de cada tool call

    # ---- seguridad de rutas -------------------------------------------
    def _p(self, rel: str) -> Path:
        p = (self.root / rel).resolve()
        if p != self.root and self.root not in p.parents:
            raise PermissionError(f"ruta fuera del worktree: {rel}")
        if ".git" in p.relative_to(self.root).parts:
            raise PermissionError(".git no se toca")
        return p

    # ---- herramientas --------------------------------------------------
    def list_files(self, path: str = ".") -> str:
        base = self._p(path)
        files = [str(f.relative_to(self.root)) for f in sorted(base.rglob("*"))
                 if f.is_file() and ".git" not in f.relative_to(self.root).parts]
        return "\n".join(files[:500]) or "(vacío)"

    def read_file(self, path: str) -> str:
        return _tail(self._p(path).read_text(encoding="utf-8"), 30000)

    def write_file(self, path: str, content: str) -> str:
        p = self._p(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return f"escrito {path} ({len(content)} chars)"

    def replace_in_file(self, path: str, old: str, new: str) -> str:
        p = self._p(path)
        text = p.read_text(encoding="utf-8")
        n = text.count(old)
        if n != 1:
            return f"ERROR: el texto a reemplazar aparece {n} veces (debe ser exactamente 1)"
        p.write_text(text.replace(old, new), encoding="utf-8")
        return f"reemplazado en {path}"

    def run_tests(self) -> str:
        r = run_cmd(self.root, self.test_command)
        return f"exit_code={r['exit_code']}\n{r['output']}"

    def run_command(self, command: str) -> str:
        if not any(command == a or command.startswith(a + " ") for a in self.allowed):
            return f"ERROR: comando no permitido. Allowlist: {self.allowed}"
        r = run_cmd(self.root, command)
        return f"exit_code={r['exit_code']}\n{r['output']}"

    def brain_search(self, query: str) -> str:
        return self.brain.search(query)

    # ---- dispatch ------------------------------------------------------
    def call(self, name: str, args: dict) -> str:
        fn = {
            "list_files": self.list_files, "read_file": self.read_file, "write_file": self.write_file,
            "replace_in_file": self.replace_in_file, "run_tests": self.run_tests,
            "run_command": self.run_command, "brain_search": self.brain_search,
        }.get(name)
        if fn is None:
            return f"ERROR: herramienta desconocida {name}"
        try:
            out = fn(**args)
        except Exception as exc:  # el error vuelve al modelo para que se corrija
            out = f"ERROR: {type(exc).__name__}: {exc}"
        self.log.append({"tool": name, "args": {k: (v[:200] if isinstance(v, str) else v) for k, v in args.items()},
                         "result": out[:300]})
        return out


def _fn(name: str, desc: str, props: dict, required: list[str]) -> dict:
    return {"type": "function", "function": {"name": name, "description": desc,
            "parameters": {"type": "object", "properties": props, "required": required}}}


S = {"type": "string"}
TOOL_SCHEMAS = [
    _fn("brain_search", "Busca en el Brain (arquitectura, convenciones, decisiones, lecciones).", {"query": S}, ["query"]),
    _fn("list_files", "Lista archivos del repo (relativo a la raíz).", {"path": S}, []),
    _fn("read_file", "Lee un archivo del repo.", {"path": S}, ["path"]),
    _fn("write_file", "Crea o sobrescribe un archivo completo.", {"path": S, "content": S}, ["path", "content"]),
    _fn("replace_in_file", "Reemplaza un fragmento único de texto en un archivo.", {"path": S, "old": S, "new": S},
        ["path", "old", "new"]),
    _fn("run_tests", "Ejecuta la suite de tests del proyecto.", {}, []),
    _fn("run_command", "Ejecuta un comando de la allowlist (lint, tests puntuales).", {"command": S}, ["command"]),
    _fn("finish", "Terminás la implementación. Resumí qué hiciste y qué tests cubren la spec.", {"summary": S}, ["summary"]),
]
