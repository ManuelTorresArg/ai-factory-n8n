"""Loop de agente con tool-calling (formato OpenAI) — lo usa el Builder.

Es el mismo patrón que Claude Code / Hermes: el modelo pide herramientas, el runner las ejecuta
en el worktree y devuelve el resultado, hasta que el modelo llama `finish` o se agota max_steps.
"""
from __future__ import annotations

import json

from .llm import LLM, msg_clean
from .tools import TOOL_SCHEMAS, Workspace


def run_agent(llm: LLM, role: str, system: str, user: str, ws: Workspace, max_steps: int = 40) -> dict:
    messages: list[dict] = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    usage = {"prompt_tokens": 0, "completion_tokens": 0}
    for step in range(1, max_steps + 1):
        msg = llm.chat(role, messages, tools=TOOL_SCHEMAS)
        for k in usage:
            usage[k] += (msg.get("_usage") or {}).get(k, 0) or 0
        messages.append(msg_clean(msg))
        calls = msg.get("tool_calls") or []
        if not calls:
            # El modelo contestó texto sin herramientas: lo empujamos a seguir o terminar.
            messages.append({"role": "user", "content": "Seguí usando herramientas, o llamá `finish` si terminaste."})
            continue
        for call in calls:
            name = call["function"]["name"]
            try:
                args = json.loads(call["function"].get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            if name == "finish":
                return {"finished": True, "summary": args.get("summary", ""), "steps": step,
                        "tool_log": ws.log, "usage": usage}
            result = ws.call(name, args)
            messages.append({"role": "tool", "tool_call_id": call.get("id", name), "content": result})
    return {"finished": False, "summary": f"max_steps ({max_steps}) agotado", "steps": max_steps,
            "tool_log": ws.log, "usage": usage}
