"""LLM simulado (FACTORY_MOCK=1) para probar el pipeline de punta a punta sin gastar tokens.

FACTORY_MOCK_REJECT_FIRST=1  → el Aprobador rechaza la iteración 1 (prueba el loop)
FACTORY_MOCK_GATE=human      → el Deploy Gate pide revisión humana
FACTORY_MOCK_BAD_REVIEW=once|always → los revisores LLM responden fuera de formato (una vez o siempre)
FACTORY_MOCK_BLOCKER=reviewer_ui → ese revisor devuelve fail con un blocker
FACTORY_MOCK_BAD_LIBRARIAN=1|partial → el Librarian responde fuera de formato | sin "decisions"
FACTORY_MOCK_LLM_DOWN=spec_writer → ese rol falla como si OmniRoute no respondiera
FACTORY_MOCK_USAGE=1000 → cada llamada reporta ese uso de tokens (prueba el presupuesto)
"""
from __future__ import annotations

import json
import os


def _call(name: str, args: dict, i: int) -> dict:
    return {"id": f"call_{i}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


def _text(content: str) -> dict:
    return {"role": "assistant", "content": content, "_usage": {}, "_model": "mock"}


def _user_text(messages: list[dict]) -> str:
    c = messages[1]["content"] if len(messages) > 1 else ""
    return c if isinstance(c, str) else " ".join(p.get("text", "") for p in c if isinstance(p, dict))


def respond(role: str, messages: list[dict], tools=None) -> dict:
    msg = _respond(role, messages, tools)
    n = int(os.environ.get("FACTORY_MOCK_USAGE", "0"))
    if n:
        msg["_usage"] = {"prompt_tokens": n - n // 4, "completion_tokens": n // 4}
    return msg


def _respond(role: str, messages: list[dict], tools=None) -> dict:
    if os.environ.get("FACTORY_MOCK_LLM_DOWN") == role:
        from .llm import LLMError
        raise LLMError(f"Rol {role} (mock): Server disconnected without sending a response.")
    if role == "spec_writer":
        return _text("# Spec\n## Objetivo\nAgregar función `greet(name)`.\n## Criterios de aceptación\n"
                     "- [ ] `greet('Ana')` devuelve `Hola, Ana`\n## Plan de tests\n- tests/test_greet.py")
    if role == "builder":
        done = sum(1 for m in messages if m.get("role") == "tool")
        if done == 0:
            return {"role": "assistant", "content": None, "_usage": {}, "tool_calls": [
                _call("brain_search", {"query": "convenciones tests"}, 1),
                _call("write_file", {"path": "tests/test_greet.py",
                                     "content": "from greet import greet\n\n\ndef test_greet():\n    assert greet('Ana') == 'Hola, Ana'\n"}, 2),
                _call("write_file", {"path": "greet.py", "content": "def greet(name: str) -> str:\n    return f'Hola, {name}'\n"}, 3),
            ]}
        if done == 3:
            return {"role": "assistant", "content": None, "_usage": {}, "tool_calls": [_call("run_tests", {}, 4)]}
        return {"role": "assistant", "content": None, "_usage": {},
                "tool_calls": [_call("finish", {"summary": "greet() implementado con test primero."}, 5)]}
    if role.startswith("reviewer_"):
        bad = os.environ.get("FACTORY_MOCK_BAD_REVIEW")  # "once" = corrige al reintentar, "always" = nunca
        if bad == "always" or (bad == "once" and len(messages) <= 2):
            return _text(json.dumps({"outcome": "passed", "summary": "todo bien", "deviations": []}))
        if os.environ.get("FACTORY_MOCK_BLOCKER") == role:
            return _text(json.dumps({"verdict": "fail", "score": 6, "summary": "hay un blocker", "findings": [
                {"severity": "blocker", "file": "greet.py", "issue": "el saludo no respeta la spec", "suggestion": "corregir"}]}))
        return _text(json.dumps({"verdict": "pass", "score": 9, "summary": f"{role}: OK", "findings": [
            {"severity": "minor", "file": "greet.py", "issue": "falta docstring", "suggestion": "agregar docstring"}]}))
    if role == "approver":
        if os.environ.get("FACTORY_MOCK_REJECT_FIRST") == "1" and "(iteración 1)" in _user_text(messages):
            return _text(json.dumps({"decision": "reject", "feedback": "Agregar docstring.", "must_fix": ["docstring en greet"]}))
        return _text(json.dumps({"decision": "approve", "feedback": "Cumple la spec.", "must_fix": []}))
    if role == "deploy_gate":
        if os.environ.get("FACTORY_MOCK_GATE") == "human":
            return _text(json.dumps({"mode": "human", "reasons": ["cambio de prueba marcado como sensible"]}))
        return _text(json.dumps({"mode": "auto", "reasons": []}))
    if role == "librarian":
        if os.environ.get("FACTORY_MOCK_BAD_LIBRARIAN") == "1":
            return _text(json.dumps({"status": "approved", "summary": "todo bien"}))
        if os.environ.get("FACTORY_MOCK_BAD_LIBRARIAN") == "partial":  # como el run real: sin "decisions" y con extras
            return _text(json.dumps({"status": "completed", "lessons": ["Copiar la plantilla existente."]}))
        return _text(json.dumps({"lessons": ["Los helpers de saludo viven en la raíz con su test en tests/."],
                                 "decisions": []}))
    return _text("{}")
