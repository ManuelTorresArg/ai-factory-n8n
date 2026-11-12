"""Brain: memoria persistente del proyecto en markdown versionable (brain/*.md).

v1 = búsqueda por palabras sobre secciones (## heading). Es deliberadamente simple y auditable:
cualquiera puede leer/editar el Brain con un editor y commitearlo. Más adelante se puede cambiar
la implementación de `search()` por pgvector sin tocar a los agentes (misma interfaz).
"""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path

CORE = ["ARCHITECTURE.md", "CONVENTIONS.md"]  # se inyectan siempre (recortados)
_WORD = re.compile(r"[a-záéíóúñü0-9_]{3,}", re.I)


class Brain:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def _sections(self):
        for f in sorted(self.root.glob("*.md")):
            current, buf = f"{f.name}", []
            for line in f.read_text(encoding="utf-8").splitlines():
                if line.startswith("## "):
                    if buf:
                        yield current, "\n".join(buf)
                    current, buf = f"{f.name} › {line[3:].strip()}", [line]
                else:
                    buf.append(line)
            if buf:
                yield current, "\n".join(buf)

    def search(self, query: str, k: int = 5) -> str:
        terms = {w.lower() for w in _WORD.findall(query)}
        scored = []
        for where, text in self._sections():
            words = [w.lower() for w in _WORD.findall(text)]
            score = sum(words.count(t) for t in terms)
            if score:
                scored.append((score, where, text))
        scored.sort(key=lambda x: -x[0])
        if not scored:
            return "Sin coincidencias en el Brain para: " + query
        return "\n\n---\n\n".join(f"[{w}]\n{t.strip()[:2500]}" for _, w, t in scored[:k])

    def context(self, limit: int = 6000) -> str:
        """Contexto base para cualquier agente: archivos core + índice de secciones del resto."""
        parts = []
        for name in CORE:
            f = self.root / name
            if f.exists():
                parts.append(f"# {name}\n{f.read_text(encoding='utf-8')[: limit // 2]}")
        index = [w for w, _ in self._sections() if not any(w.startswith(c) for c in CORE)]
        if index:
            parts.append("# Otras secciones del Brain (consultables con brain_search)\n- " + "\n- ".join(index[:80]))
        return "\n\n".join(parts)[:limit]

    def append(self, filename: str, heading: str, lines: list[str], run_id: str) -> None:
        if not lines:
            return
        f = self.root / filename
        block = f"\n## {heading} ({date.today().isoformat()}, run {run_id})\n" + "\n".join(f"- {l}" for l in lines) + "\n"
        with open(f, "a", encoding="utf-8") as fh:
            fh.write(block)
