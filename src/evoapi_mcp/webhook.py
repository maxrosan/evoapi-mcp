"""Recebe e resume eventos da Evolution API, para descobrir o que ela entrega.

Esta é a fase de observação do bot "IA:": o endpoint apenas registra o que chega,
sem responder nada e sem chamar modelo nenhum. Serve para responder à única
pergunta que pode derrubar o plano: a Evolution avisa quando a mensagem é enviada
pelo próprio dono da instância, digitada no celular dele?

Privacidade: o resumo guarda no máximo PREVIEW_CHARS caracteres do texto e nunca
o conteúdo de mídia. O histórico fica só em memória, some quando o processo
reinicia e não vai para disco.
"""

from __future__ import annotations

import sys
from collections import deque
from datetime import datetime
from typing import Any

from evoapi_mcp.formatters import clean, compact_message, jid_to_number

# Prefixo que, no futuro, aciona o bot. Aqui só é sinalizado.
TRIGGER_PREFIX = "ia:"
PREVIEW_CHARS = 80
MAX_EVENTS = 200


def _log(message: str) -> None:
    print(f"[INFO] Webhook: {message}", file=sys.stderr)


def is_trigger(text: str | None) -> bool:
    """True se o texto começa com o prefixo, ignorando caixa e espaços.

    Cuidado: isto olha só o texto. Quem decide o acionamento é `summarize_event`,
    que exige também que a mensagem seja do dono da instância.
    """
    return bool(text) and text.lstrip().casefold().startswith(TRIGGER_PREFIX)


def instruction_of(text: str | None) -> str | None:
    """Devolve o que vem depois de 'IA:', ou None se não for um acionamento."""
    if not is_trigger(text):
        return None
    return text.lstrip()[len(TRIGGER_PREFIX):].strip() or None


def summarize_event(payload: Any) -> dict[str, Any]:
    """Reduz um evento da Evolution ao que interessa para esta investigação.

    Campos: event, instance, at, from_me, chat, chat_type, type, preview,
    trigger, message_id.
    """
    if not isinstance(payload, dict):
        return {"event": "?", "erro": "corpo não é um objeto JSON"}

    evento = payload.get("event") or "?"
    dados = payload.get("data")
    resumo: dict[str, Any] = {
        "event": evento,
        "instance": payload.get("instance"),
        "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

    if not isinstance(dados, dict):
        return clean(resumo)

    key = dados.get("key") or {}
    jid = key.get("remoteJid") or dados.get("remoteJid")
    compacta = compact_message(dados, max_text=None) if dados.get("message") or key else {}
    texto = compacta.get("text")
    minha = bool(key.get("fromMe")) if key else None

    # Acionamento exige as duas coisas: ser minha E começar com o prefixo. Um
    # terceiro escrevendo "IA:" num grupo não pode comandar nada.
    aciona = bool(minha) and is_trigger(texto)

    resumo.update({
        "message_id": key.get("id"),
        "from_me": minha,
        "chat": jid_to_number(jid) if jid else None,
        "chat_type": ("grupo" if str(jid).endswith("@g.us") else "direto") if jid else None,
        "type": compacta.get("type") or dados.get("messageType"),
        "preview": (texto[:PREVIEW_CHARS] + "…") if texto and len(texto) > PREVIEW_CHARS else texto,
        "trigger": True if aciona else None,
    })
    return clean(resumo)


class EventLog:
    """Últimos eventos recebidos, em memória."""

    def __init__(self, maxlen: int = MAX_EVENTS):
        self._eventos: deque[dict[str, Any]] = deque(maxlen=maxlen)
        self.total = 0
        self.started = datetime.now()

    def add(self, payload: Any) -> dict[str, Any]:
        resumo = summarize_event(payload)
        self._eventos.append(resumo)
        self.total += 1
        marca = " <<< ACIONAMENTO" if resumo.get("trigger") else ""
        _log(
            f"{resumo.get('event')} from_me={resumo.get('from_me')} "
            f"tipo={resumo.get('type')} chat={resumo.get('chat_type')}{marca}"
        )
        return resumo

    def snapshot(self, limit: int = 50, only_mine: bool = False, only_triggers: bool = False) -> dict[str, Any]:
        eventos = list(self._eventos)
        if only_mine:
            eventos = [e for e in eventos if e.get("from_me")]
        if only_triggers:
            eventos = [e for e in eventos if e.get("trigger")]

        por_evento: dict[str, int] = {}
        for e in self._eventos:
            por_evento[e.get("event", "?")] = por_evento.get(e.get("event", "?"), 0) + 1

        return {
            "desde": self.started.strftime("%Y-%m-%d %H:%M:%S"),
            "total_recebido": self.total,
            "em_memoria": len(self._eventos),
            "por_evento": por_evento,
            "minhas_mensagens": sum(1 for e in self._eventos if e.get("from_me")),
            "acionamentos": sum(1 for e in self._eventos if e.get("trigger")),
            "eventos": eventos[-limit:],
        }
