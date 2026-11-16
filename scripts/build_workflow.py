"""Genera n8n/ai-factory.workflow.json (importable en n8n ≥ 1.49).

    python scripts/build_workflow.py

Editá el pipeline acá y regenerá: mantener el JSON a mano es frágil.
"""
import json
import uuid
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "n8n" / "ai-factory.workflow.json"
RID = "{{ $('Create Run').first().json.run_id }}"
BASE = "={{ $env.FACTORY_RUNNER_URL }}"
HEADERS = {"parameters": [{"name": "X-Factory-Token", "value": "={{ $env.FACTORY_TOKEN }}"}]}

nodes, connections = [], {}
STAGES = []  # nodos cuya rama de error termina en "Stage Error"


def nid(name):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "ai-factory/" + name))


def http(name, path, pos, body=None, execute_once=False, timeout_min=30, on_error=True):
    params = {"method": "POST", "url": f"{BASE}{path}", "sendHeaders": True, "headerParameters": HEADERS,
              "options": {"timeout": timeout_min * 60 * 1000}}
    if body:
        params |= {"sendBody": True, "specifyBody": "json", "jsonBody": body}
    node = {"id": nid(name), "name": name, "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2,
            "position": pos, "parameters": params}
    if execute_once:
        node["executeOnce"] = True
    if on_error:
        # Salida 1 = error (HTTP ≥ 400, timeout): va a "Stage Error" y el run queda en failed, no colgado.
        node["onError"] = "continueErrorOutput"
        STAGES.append(name)
    nodes.append(node)


def if_true(name, expr, pos):
    nodes.append({"id": nid(name), "name": name, "type": "n8n-nodes-base.if", "typeVersion": 2.2, "position": pos,
                  "parameters": {"conditions": {
                      "options": {"caseSensitive": True, "leftValue": "", "typeValidation": "loose", "version": 2},
                      "conditions": [{"id": nid(name + "/c"), "leftValue": expr, "rightValue": "",
                                      "operator": {"type": "boolean", "operation": "true", "singleValue": True}}],
                      "combinator": "and"}, "options": {}}})


def link(src, dst, out=0, inp=0):
    main = connections.setdefault(src, {"main": []})["main"]
    while len(main) <= out:
        main.append([])
    main[out].append({"node": dst, "type": "main", "index": inp})


# ---- nodos -------------------------------------------------------------
nodes.append({"id": nid("Intake"), "name": "Intake", "type": "n8n-nodes-base.webhook", "typeVersion": 2,
              "position": [0, 300], "webhookId": nid("webhook"),
              "parameters": {"httpMethod": "POST", "path": "ai-factory", "responseMode": "onReceived", "options": {}}})
http("Create Run", "/runs", [220, 300], body="={{ JSON.stringify($json.body) }}", timeout_min=1, on_error=False)
http("Spec Writer", f"/runs/{RID}/spec", [440, 300], timeout_min=15)
http("Builder", f"/runs/{RID}/build", [660, 300], timeout_min=45)
reviewers = ["Security", "UX", "UI", "Code", "Load"]  # Load = Locust (determinista)
for i, r in enumerate(reviewers):
    http(f"Review {r}", f"/runs/{RID}/review/{r.lower()}", [900, 20 + i * 140], timeout_min=15)
nodes.append({"id": nid("Wait Reviews"), "name": "Wait Reviews", "type": "n8n-nodes-base.merge", "typeVersion": 3,
              "position": [1140, 300], "parameters": {"numberInputs": len(reviewers)}})
http("Approver", f"/runs/{RID}/approve", [1360, 300], execute_once=True, timeout_min=15)
if_true("Approved?", "={{ $json.approved }}", [1580, 300])
if_true("Retry?", "={{ $json.can_retry }}", [1800, 480])
http("Mark Failed", f"/runs/{RID}/fail", [2020, 580], body='={{ JSON.stringify({reason: "max_iterations"}) }}',
     timeout_min=1, on_error=False)
http("Stage Error", f"/runs/{RID}/fail", [1360, 760], timeout_min=1, on_error=False,
     body="={{ JSON.stringify({reason: $prevNode.name + ': ' + ($json.error?.message || $json.error?.description || JSON.stringify($json.error || $json)).slice(0, 800)}) }}")
http("Deploy Gate", f"/runs/{RID}/gate", [1800, 200], timeout_min=15)
http("Deploy", f"/runs/{RID}/deploy", [2020, 200], timeout_min=5)
# El Librarian corre con el run ya cerrado (merge, PR o failed): si falla, no tiene sentido marcarlo fallido.
http("Librarian", f"/runs/{RID}/learn", [2240, 300], execute_once=True, timeout_min=15, on_error=False)
if_true("Needs Human?", "={{ $('Deploy').isExecuted && $('Deploy').first().json.status === 'awaiting_human' }}", [2460, 300])
nodes.append({"id": nid("Notify"), "name": "Notify human (Telegram/Teams)", "type": "n8n-nodes-base.noOp",
              "typeVersion": 1, "position": [2680, 200], "parameters": {}})
nodes.append({"id": nid("Done"), "name": "Done", "type": "n8n-nodes-base.noOp", "typeVersion": 1,
              "position": [2680, 400], "parameters": {}})
nodes.append({"id": nid("note"), "name": "Sticky Note", "type": "n8n-nodes-base.stickyNote", "typeVersion": 1,
              "position": [-40, -160], "parameters": {"width": 640, "height": 250, "content":
              "## AI Factory\nPOST /webhook/ai-factory `{title, description, source, source_ref, ui_url}`\n\n"
              "Spec → Builder (TDD) → 5 revisores en paralelo (4 LLM + Locust) → Aprobador.\n"
              "Rechazo + iteraciones disponibles ⇒ vuelve al Builder con el feedback.\n"
              "Aprobado ⇒ Deploy Gate (reglas de path + agente) ⇒ merge local o PR.md para humano.\n\n"
              "Si una etapa falla (LLM caído, timeout, 4xx/5xx) ⇒ **Stage Error** marca el run como failed.\n"
              "Reemplazá **Notify human** por un nodo Telegram o Microsoft Teams."}})

# ---- conexiones --------------------------------------------------------
link("Intake", "Create Run")
link("Create Run", "Spec Writer")
link("Spec Writer", "Builder")
for i, r in enumerate(reviewers):
    link("Builder", f"Review {r}")
    link(f"Review {r}", "Wait Reviews", inp=i)
link("Wait Reviews", "Approver")
link("Approver", "Approved?")
link("Approved?", "Deploy Gate", out=0)
link("Approved?", "Retry?", out=1)
link("Retry?", "Builder", out=0)          # ← el loop: vuelve al Builder con el feedback del Aprobador
link("Retry?", "Mark Failed", out=1)
link("Mark Failed", "Librarian")
link("Deploy Gate", "Deploy")
link("Deploy", "Librarian")
link("Librarian", "Needs Human?")
link("Needs Human?", "Notify human (Telegram/Teams)", out=0)
link("Needs Human?", "Done", out=1)
for name in STAGES:
    link(name, "Stage Error", out=1)

wf = {"name": "AI Factory", "nodes": nodes, "connections": connections, "pinData": {},
      "settings": {"executionOrder": "v1"}, "active": False, "meta": {"templateCredsSetupCompleted": True}}

# validación estructural
names = {n["name"] for n in nodes}
assert len(names) == len(nodes), "nombres duplicados"
for src, c in connections.items():
    assert src in names, src
    for outs in c["main"]:
        for t in outs:
            assert t["node"] in names, t
OUT.write_text(json.dumps(wf, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
print(f"OK {OUT} — {len(nodes)} nodos")
