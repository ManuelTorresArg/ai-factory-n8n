"""Driver secuencial SIN n8n — misma lógica que el workflow, útil para depurar una etapa.

    python -m app.cli run "Título" "Descripción"
    python -m app.cli status <run_id>

En operación normal el orquestador es n8n; esto es el equivalente a correr un job de CI a mano.
"""
from __future__ import annotations

import json
import sys
from concurrent.futures import ThreadPoolExecutor

from .stages import ALL_REVIEWS, Factory


def run_pipeline(f: Factory, title: str, description: str = "", **extra) -> dict:
    rid = f.intake({"title": title, "description": description, **extra})["run_id"]
    print("intake", rid)
    print("spec  ", f.spec(rid))
    while True:
        print("build ", f.build(rid))
        with ThreadPoolExecutor(max_workers=len(ALL_REVIEWS)) as pool:  # = rama paralela de n8n
            for r in pool.map(lambda k: f.review(rid, k), ALL_REVIEWS):
                print("review", r)
        a = f.approve(rid)
        print("approve", a)
        if a["approved"]:
            break
        if not a["can_retry"]:
            f.fail(rid, "máximo de iteraciones alcanzado")
            print("learn ", f.learn(rid))
            return f.store.load(rid)
    print("gate  ", f.gate(rid))
    print("deploy", f.deploy(rid))
    print("learn ", f.learn(rid))
    return f.store.load(rid)


def main(argv: list[str]) -> None:
    if len(argv) >= 2 and argv[0] == "run":
        run = run_pipeline(Factory(), argv[1], argv[2] if len(argv) > 2 else "")
        print(json.dumps({k: run[k] for k in ("id", "status", "iteration", "branch", "deploy")}, indent=2, ensure_ascii=False))
    elif len(argv) == 2 and argv[0] == "status":
        print(json.dumps(Factory().store.load(argv[1]), indent=2, ensure_ascii=False))
    else:
        print(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
