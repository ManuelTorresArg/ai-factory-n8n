"""Cliente OpenAI-compatible contra OmniRoute (/v1/chat/completions).

Cada rol de factory.yaml apunta a un combo de OmniRoute (ej. hermes-coder, hermes-qa).
El runner no sabe qué proveedor hay detrás: OmniRoute hace de "ingress" de modelos,
como un Ingress de K8s que enruta a distintos Services según la regla.
"""
from __future__ import annotations

import json
import os
import re
import time

import httpx

from .config import role_cfg


class LLMError(RuntimeError):
    pass


class LLM:
    def __init__(self, cfg: dict, mock: bool = False) -> None:
        om = cfg.get("omniroute", {})
        self.base_url = (om.get("base_url") or "http://localhost:20128/v1").rstrip("/")
        self.api_key = om.get("api_key") or os.environ.get("OMNIROUTE_API_KEY", "sk-local")
        self.timeout = float(om.get("timeout_s", 600))
        self.retries = int(om.get("retries", 2))
        self.cfg = cfg
        self.mock = mock

    def chat(self, role: str, messages: list[dict], tools: list[dict] | None = None) -> dict:
        """Devuelve el `message` del assistant (con content y/o tool_calls) + meta de uso."""
        rc = role_cfg(self.cfg, role)
        if self.mock:
            from .mock import respond
            return respond(role, messages, tools)

        body = {"model": rc["model"], "messages": messages, "temperature": rc.get("temperature", 0.2)}
        if rc.get("max_tokens"):
            body["max_tokens"] = rc["max_tokens"]
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"

        last = None
        for attempt in range(self.retries + 1):
            try:
                with httpx.Client(timeout=self.timeout) as client:
                    r = client.post(
                        f"{self.base_url}/chat/completions",
                        headers={"Authorization": f"Bearer {self.api_key}"},
                        json=body,
                    )
                if r.status_code >= 500 or r.status_code == 429:
                    raise LLMError(f"OmniRoute {r.status_code}: {r.text[:300]}")
                r.raise_for_status()
                data = r.json()
                msg = data["choices"][0]["message"]
                msg["_usage"] = data.get("usage", {})
                msg["_model"] = data.get("model", rc["model"])
                return msg
            except (httpx.HTTPError, LLMError, KeyError) as exc:
                last = exc
                time.sleep(2 * (attempt + 1))
        raise LLMError(f"Rol {role} ({rc['model']}): {last}")

    def ask_json(self, role: str, system: str, user: str | list, check=None) -> dict:
        """Pide una respuesta JSON y la parsea de forma tolerante (no todos los combos soportan json_mode).

        `check(dict) -> str | None` valida la forma: si devuelve un error, se le pide al modelo UNA vez
        que corrija. El llamador vuelve a validar, porque el segundo intento también puede venir mal.
        """
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        msg = self.chat(role, messages)
        try:
            out = parse_json(msg.get("content") or "")
        except ValueError:
            messages += [msg_clean(msg), {"role": "user", "content":
                         "Tu respuesta no fue JSON válido. Devolvé SOLO el objeto JSON pedido."}]
            msg = self.chat(role, messages)
            out = parse_json(msg.get("content") or "")
        problem = check(out) if check else None
        if problem:
            messages += [msg_clean(msg), {"role": "user", "content":
                         f"Tu JSON no respeta el formato pedido: {problem}. "
                         "Devolvé SOLO el objeto JSON con exactamente las claves del formato del final de tus instrucciones."}]
            try:
                out = parse_json(self.chat(role, messages).get("content") or "")
            except ValueError:
                pass
        return out

    def ask_text(self, role: str, system: str, user: str) -> str:
        msg = self.chat(role, [{"role": "system", "content": system}, {"role": "user", "content": user}])
        return (msg.get("content") or "").strip()


def msg_clean(msg: dict) -> dict:
    """Quita las claves internas (_usage, _model) antes de reenviar el mensaje al modelo."""
    return {k: v for k, v in msg.items() if not k.startswith("_")}


def parse_json(text: str) -> dict:
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    candidate = fence.group(1) if fence else text
    start, end = candidate.find("{"), candidate.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("sin JSON")
    return json.loads(candidate[start:end + 1])
