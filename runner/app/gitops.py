"""Operaciones git. Cada run trabaja en su propio `git worktree` + rama factory/<run_id>.

Analogía: el repo de trabajo es como un host Docker y cada worktree es un contenedor aislado
sobre la misma "imagen" (el historial): varios runs pueden construir en paralelo sin pisarse,
y tu checkout local queda intacto hasta el merge.
"""
from __future__ import annotations

import fnmatch
import subprocess
from pathlib import Path


class GitError(RuntimeError):
    pass


def git(cwd: Path, *args: str, check: bool = True) -> str:
    p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if check and p.returncode != 0:
        raise GitError(f"git {' '.join(args)}: {p.stderr.strip() or p.stdout.strip()}")
    return p.stdout.strip()


def ensure_repo(repo: Path, base: str) -> None:
    if not (repo / ".git").exists():
        raise GitError(f"{repo} no es un repo git (montá tu proyecto en /workspace)")
    git(repo, "rev-parse", "--verify", base)


def create_worktree(repo: Path, wt: Path, branch: str, base: str) -> None:
    wt.parent.mkdir(parents=True, exist_ok=True)
    if wt.exists():
        return
    git(repo, "worktree", "add", "-b", branch, str(wt), base)


def commit_all(wt: Path, message: str) -> str | None:
    git(wt, "add", "-A")
    if not git(wt, "status", "--porcelain"):
        return None
    git(wt, "commit", "-m", message)
    return git(wt, "rev-parse", "--short", "HEAD")


def diff(wt: Path, base: str, max_chars: int = 60000) -> str:
    out = git(wt, "diff", f"{base}...HEAD")
    return out if len(out) <= max_chars else out[:max_chars] + f"\n\n[... diff truncado, {len(out)} chars en total]"


def changed_files(wt: Path, base: str) -> list[str]:
    out = git(wt, "diff", "--name-only", f"{base}...HEAD")
    return [l for l in out.splitlines() if l]


def lines_changed(wt: Path, base: str) -> int:
    total = 0
    for line in git(wt, "diff", "--numstat", f"{base}...HEAD").splitlines():
        a, d, _ = line.split("\t", 2)
        total += (int(a) if a.isdigit() else 0) + (int(d) if d.isdigit() else 0)
    return total


def tree(wt: Path, limit: int = 300) -> str:
    files = git(wt, "ls-files").splitlines()
    more = f"\n... (+{len(files) - limit} archivos)" if len(files) > limit else ""
    return "\n".join(files[:limit]) + more


def match_any(path: str, patterns: list[str]) -> str | None:
    for pat in patterns:
        if fnmatch.fnmatch(path, pat) or fnmatch.fnmatch("/" + path, "*/" + pat.lstrip("*/")):
            return pat
    return None


def merge_into_base(repo: Path, branch: str, base: str, message: str) -> str:
    """Merge local --no-ff. Sólo si tu checkout está en `base` y limpio: nunca toca trabajo sin commitear."""
    current = git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    if current != base:
        raise GitError(f"el repo está en '{current}', no en '{base}'")
    if git(repo, "status", "--porcelain", "--untracked-files=no"):
        raise GitError("el repo tiene cambios sin commitear")
    git(repo, "merge", "--no-ff", branch, "-m", message)
    return git(repo, "rev-parse", "--short", "HEAD")


def remove_worktree(repo: Path, wt: Path) -> None:
    git(repo, "worktree", "remove", "--force", str(wt), check=False)
    git(repo, "worktree", "prune", check=False)
