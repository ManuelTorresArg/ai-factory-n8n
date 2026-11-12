"""Carga de factory.yaml con expansión de ${VAR} / ${VAR:-default} desde el entorno."""
from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path

import yaml

_ENV = re.compile(r"\$\{([A-Z0-9_]+)(?::-([^}]*))?\}")


def _expand(value):
    if isinstance(value, str):
        return _ENV.sub(lambda m: os.environ.get(m.group(1), m.group(2) or ""), value)
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand(v) for v in value]
    return value


class Settings:
    """Rutas del runner. Todo se puede pisar por variable de entorno (igual que en compose)."""

    def __init__(self) -> None:
        here = Path(__file__).resolve().parents[2]  # raíz del repo ai-factory
        self.config_path = Path(os.environ.get("FACTORY_CONFIG", here / "factory.yaml"))
        self.workspace = Path(os.environ.get("WORKSPACE", "/workspace"))
        self.data_dir = Path(os.environ.get("DATA_DIR", here / "data"))
        self.brain_dir = Path(os.environ.get("BRAIN_DIR", here / "brain"))
        self.prompts_dir = Path(os.environ.get("PROMPTS_DIR", here / "prompts"))
        self.token = os.environ.get("FACTORY_TOKEN", "")
        self.mock = os.environ.get("FACTORY_MOCK", "0") == "1"

    @property
    def runs_dir(self) -> Path:
        return self.data_dir / "runs"

    @property
    def worktrees_dir(self) -> Path:
        return self.data_dir / "worktrees"


@lru_cache
def settings() -> Settings:
    return Settings()


def load_config(path: Path | None = None) -> dict:
    path = path or settings().config_path
    with open(path, encoding="utf-8") as fh:
        return _expand(yaml.safe_load(fh))


def role_cfg(cfg: dict, role: str) -> dict:
    roles = cfg.get("roles", {})
    if role not in roles:
        raise KeyError(f"Rol '{role}' no definido en factory.yaml > roles")
    return roles[role]
