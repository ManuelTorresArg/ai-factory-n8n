# AI Factory (local)

Pipeline de desarrollo autónomo, auditable y repetible: una tarea entra por un webhook y sale como un merge a `main` o como un PR esperando a un humano.

```
Intake → Spec Writer → Builder (TDD) ⇄ [Security · UX · UI · Code · Carga/Locust] → Aprobador → Deploy Gate → Deploy → Librarian
                              ▲                                          │ rechazo (≤ max_iterations)
                              └──────────────────────────────────────────┘
```

| Pieza | Qué es | Analogía en tu stack |
|---|---|---|
| **n8n** | Orquestador: el workflow con las etapas, el paralelismo y el loop | El `.gitlab-ci.yml` (stages + `needs:`) |
| **factory-runner** | API FastAPI que ejecuta cada etapa (un endpoint por etapa) | El `gitlab-runner` que corre los jobs |
| **OmniRoute** | Router de modelos; cada rol apunta a un combo | Un Ingress que enruta a distintos Services |
| **Brain** (`brain/*.md`) | Memoria persistente que leen todos los agentes y actualiza el Librarian | Un ConfigMap versionado que todos los pods montan |
| **git worktree** por run | Aislamiento: cada run construye en su carpeta y rama `factory/<run_id>` | Un contenedor por job sobre la misma imagen |
| **data/runs/<id>/** | `run.json` + `audit.jsonl` + artefactos por etapa | Los logs y artifacts de cada job |

## Puesta en marcha

```bash
cp .env.example .env            # TARGET_REPO, FACTORY_TOKEN, UID/GID, combos
docker compose up -d --build
```

1. **OmniRoute alcanzable desde Docker.** OmniRoute escucha sólo en `127.0.0.1:20128`, así que el servicio `omniroute-bridge` (socat, `network_mode: host`) lo reexpone sólo en `172.17.0.1:20129`. Es como un dst-nat limitado a la interfaz de Docker: los contenedores lo ven y la LAN no.
   Prueba: `docker compose exec factory-runner python -c "import httpx;r=httpx.get('http://host.docker.internal:20129/v1/models',timeout=5);print(r.status_code, len(r.json()['data']))"` → `200 <n modelos>`.
   Si falla con *Cannot assign requested address*, tu docker0 tiene otra IP: `ip -4 addr show docker0` y ajustá `DOCKER_HOST_IP`.
2. **Importá y publicá el workflow** (n8n 2.x usa *publish* en lugar de *activate*):
   ```bash
   docker compose exec n8n n8n import:workflow --input=/workflows/ai-factory.workflow.json
   docker compose exec n8n n8n list:workflow             # anotá el ID
   docker compose exec n8n n8n publish:workflow --id=<ID>
   docker compose restart n8n
   ```
   (O desde la UI en http://localhost:5678: *Import from file* → *Publish*.)
3. **Primera prueba sin gastar tokens:** `FACTORY_MOCK=1` en `.env`, `docker compose up -d`, y
   ```bash
   ./scripts/submit.sh "Saludo" "Agregar greet(name) que devuelva 'Hola, <name>'"
   curl -s -H "X-Factory-Token: $FACTORY_TOKEN" localhost:8000/runs | jq
   ```
   Después `FACTORY_MOCK=0` y a trabajar con modelos reales.
4. **Completá el Brain** (`brain/ARCHITECTURE.md`, `brain/CONVENTIONS.md`) con la realidad de tu repo. Es lo que más mejora la calidad de las specs.

## Operación

| Acción | Cómo |
|---|---|
| Enviar tarea | `./scripts/submit.sh "título" "descripción" [sentry|github|mantis] [ref]` o `POST /webhook/ai-factory` |
| Ver runs | `GET localhost:8000/runs` · detalle `GET /runs/<id>` · bitácora `GET /runs/<id>/audit` |
| Aprobar un PR humano | Leer `data/runs/<id>/PR.md` y `git diff main...factory/<id>`, luego `POST /runs/<id>/human-decision {"approve": true}` |
| Depurar sin n8n | `docker compose exec factory-runner python -m app.cli run "título" "descripción"` |
| API interactiva | http://localhost:8000/docs |
| Tests de la fábrica | `docker compose exec factory-runner python -m pytest -q tests` |

## Guardarraíles (deterministas, el LLM no los puede saltear)

- Los tests los vuelve a correr la fábrica después del Builder; rojo ⇒ rechazo.
- Si el diff no toca ningún test ⇒ rechazo (TDD obligatorio).
- Revisor de carga (Locust): p95, tasa de errores o req/s fuera de umbral, o p95 más de `max_regression_pct` por encima del último merge ⇒ rechazo.
- Paths en `deploy.critical_paths` o más de `max_auto_lines` líneas ⇒ revisión humana.
- Merge automático sólo si tu checkout está en `main` y sin cambios sin commitear; si no, pasa a humano.
- Builder confinado a su worktree, `.git` intocable, comandos por allowlist sin shell.
- `max_iterations` corta el loop Builder ⇄ Aprobador.

## Prueba de carga (Locust)

Quinto revisor, en paralelo con los otros cuatro. No usa LLM: mide, y el veredicto sale de `factory.yaml > loadtest`.

1. Copiá `scripts/locustfile.example.py` a `loadtest/locustfile.py` en tu repo. Sin ese archivo, el revisor pasa como "no aplica".
2. Definí cómo levantar la app dentro del worktree, con `{port}` como puerto libre del run:
   ```yaml
   loadtest:
     start_command: "python -m uvicorn app.main:app --port {port}"
     health_path: /health
   ```
   Si lo dejás vacío, Locust apunta a `target_url` (un servicio que ya está corriendo; nunca producción).
3. Ajustá los umbrales: `p95_ms`, `failure_ratio`, `min_rps` y `max_regression_pct`.

Cada merge guarda sus métricas en `data/loadtest-baseline.json`, y el próximo run se compara contra eso. Los CSV de Locust y el log de la app quedan en `data/runs/<id>/` (`locust-<n>_stats.csv`, `loadtest-app-<n>.log`). El Spec Writer agrega una sección "Carga" cuando hay endpoints, y el Builder mantiene el locustfile.

## Actualizar el workflow en n8n

`import:workflow` crea un workflow **nuevo**, no pisa el anterior, y dos workflows no pueden publicar el mismo webhook. Primero despublicá el viejo y después publicá el nuevo:
```bash
docker compose exec n8n n8n unpublish:workflow --id=<ID_VIEJO>
docker compose exec n8n n8n import:workflow --input=/workflows/ai-factory.workflow.json
docker compose exec n8n n8n publish:workflow --id=<ID_NUEVO>
docker compose restart n8n
```

## Personalización

- **Modelos:** `factory.yaml > roles` o las variables `COMBO_*` de `.env`. Ideal: combos dedicados en OmniRoute (`factory-architect` para spec/aprobador/gate, `factory-reviewer`, uno con visión para UI).
- **Prompts:** `prompts/*.md`. Los cuatro revisores LLM comparten el formato de `_review_format.md` (si lo cambiás, actualizá también el final de cada `reviewer_*.md`).
- **Workflow:** editá `scripts/build_workflow.py` y regenerá con `python scripts/build_workflow.py`.
- **Proyectos JS:** `WITH_NODE=true` y `test_command: "npm test"`. **Revisor UI con capturas:** `WITH_PLAYWRIGHT=true`, combo con visión en `COMBO_VISION` y `ui_url` en la tarea.
- **Notificación:** reemplazá el nodo *Notify human* por Telegram o Teams.

## Próximo paso: escalar a GitLab (REPIA)

Cada pieza local tiene su equivalente: webhook de issues/MR de GitLab como Intake, cada etapa como un CI/CD Component en `platform/ci-components`, la rama `factory/<id>` pusheada y `deploy` creando un MR con *merge when pipeline succeeds* en lugar del merge local. Ver el doc de diseño.
