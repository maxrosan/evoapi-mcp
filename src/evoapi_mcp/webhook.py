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

import os
import sys
import time
from collections import deque
from datetime import datetime
from typing import Any, Callable

from evoapi_mcp.formatters import clean, compact_message, jid_to_number
from evoapi_mcp.store import build_store

# Prefixo que, no futuro, aciona o bot. Aqui só é sinalizado.
TRIGGER_PREFIX = "ia:"
PREVIEW_CHARS = 80
MAX_EVENTS = 200
# A instrução é guardada inteira, e não cortada como a prévia: é o texto que o
# assistente precisa executar. São palavras do próprio dono, nunca de terceiro.
MAX_INSTRUCTION = 2000
# Últimos dígitos comparados para reconhecer o próprio número. Oito porque o WhatsApp
# escreve o mesmo telefone ora com o nono dígito, ora sem.
TAIL_DIGITS = 8


def _log(message: str) -> None:
    print(f"[INFO] Webhook: {message}", file=sys.stderr)


def is_trigger(text: str | None) -> bool:
    """True se o texto começa com o prefixo, ignorando caixa e espaços.

    Cuidado: isto olha só o texto. Quem decide o acionamento é `summarize_event`,
    que exige também que a mensagem seja do dono da instância.
    """
    return bool(text) and text.lstrip().casefold().startswith(TRIGGER_PREFIX)


def instruction_of(text: str | None, self_chat: bool = False) -> str | None:
    """A instrução contida na mensagem, ou None se não houver.

    Na conversa do dono com ele mesmo, a mensagem inteira é a instrução: ali não se
    escreve prefixo, tudo o que ele manda é pedido. Nas outras conversas, só conta o
    que vem depois de "IA:".
    """
    if self_chat:
        return (text or "").strip()[:MAX_INSTRUCTION] or None
    if not is_trigger(text):
        return None
    return text.lstrip()[len(TRIGGER_PREFIX):].strip()[:MAX_INSTRUCTION] or None


def _tail(value: str | None) -> str:
    """Os últimos dígitos de um número ou jid, para comparação tolerante."""
    digitos = "".join(c for c in (value or "") if c.isdigit())
    return digitos[-TAIL_DIGITS:] if len(digitos) >= TAIL_DIGITS else ""


def is_self_chat(remote_jid: str | None, alt_jid: str | None, owner_number: str | None) -> bool:
    """True quando a conversa é a do dono com ele mesmo.

    O jid dela costuma ser opaco (`...@lid`), então o telefone aparece em
    `remoteJidAlt`. Compara pelos últimos dígitos porque o mesmo número aparece
    ora com o nono dígito, ora sem.
    """
    dono = _tail(owner_number)
    if not dono:
        return False
    if remote_jid and str(remote_jid).endswith("@g.us"):
        return False
    return dono in (_tail(remote_jid), _tail(alt_jid))


def quoted_of(dados: dict[str, Any]) -> dict[str, Any] | None:
    """A mensagem citada numa resposta, ou None. Evolution põe o contextInfo ora
    no topo de `data`, ora dentro do tipo da mensagem (extendedTextMessage...)."""
    ctx = dados.get("contextInfo")
    if not isinstance(ctx, dict) or not ctx.get("stanzaId"):
        ctx = None
        for valor in (dados.get("message") or {}).values():
            if isinstance(valor, dict) and isinstance(valor.get("contextInfo"), dict) \
                    and valor["contextInfo"].get("stanzaId"):
                ctx = valor["contextInfo"]
                break
    if not ctx:
        return None
    citada = ctx.get("quotedMessage") or {}
    texto = citada.get("conversation") or (citada.get("extendedTextMessage") or {}).get("text")
    if not texto:
        texto = next((v.get("caption") for v in citada.values() if isinstance(v, dict) and v.get("caption")), None)
    return {"id": ctx["stanzaId"], "text": texto, "participant": ctx.get("participant")}


def summarize_event(payload: Any, owner_number: str | None = None,
                    replied_to_us: Callable[[str], bool] | None = None) -> dict[str, Any]:
    """Reduz um evento da Evolution ao que interessa.

    Campos: event, instance, at, from_me, chat, chat_type, self_chat, type,
    preview, instruction, trigger, message_id.
    """
    if not isinstance(payload, dict):
        return {"event": "?", "erro": "corpo não é um objeto JSON"}

    evento = payload.get("event") or "?"
    dados = payload.get("data")
    resumo: dict[str, Any] = {
        "event": evento,
        "instance": payload.get("instance"),
        "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        # Carimbo numérico para medir idade sem parsear texto (o vigia usa isto).
        "ts": time.time(),
    }

    if not isinstance(dados, dict):
        return clean(resumo)

    key = dados.get("key") or {}
    jid = key.get("remoteJid") or dados.get("remoteJid")
    compacta = compact_message(dados, max_text=None) if dados.get("message") or key else {}
    texto = compacta.get("text")
    minha = bool(key.get("fromMe")) if key else None
    propria = is_self_chat(jid, key.get("remoteJidAlt"), owner_number)

    # Duas portas, e as duas exigem que a mensagem seja do dono:
    #  - na conversa dele com ele mesmo, tudo o que ele escreve é instrução;
    #  - nas demais, só o que começa com "IA:".
    # Um terceiro escrevendo "IA:" num grupo continua não comandando nada.
    # Reação NUNCA aciona. O texto de uma reação é o próprio emoji, e na conversa
    # pessoal toda mensagem do dono é instrução: sem esta linha, um 👍 do Max viraria
    # a instrução "👍", e o 👀 que o próprio bot usa para dizer "estou fazendo"
    # voltaria como um novo acionamento.
    tipo = compacta.get("type") or dados.get("messageType")
    e_reacao = tipo in ("reaction", "reactionMessage")

    instrucao = instruction_of(texto, self_chat=propria) if minha and not e_reacao else None
    aciona = bool(minha) and bool(instrucao) and (propria or is_trigger(texto))

    # Terceira porta: o dono respondendo, com citação, a uma mensagem do assistente.
    # Quando o assistente pergunta algo num chat com terceiro e Max responde citando
    # a pergunta, é uma resposta a ele, não uma mensagem para o terceiro, mesmo sem
    # "IA:". Só vale se a mensagem citada for conhecida (enviada ou tratada por nós):
    # citar uma mensagem qualquer continua não acionando nada.
    citada = quoted_of(dados)
    responde_ao_assistente = bool(
        minha and not e_reacao and citada and texto and replied_to_us and replied_to_us(citada["id"])
    )
    if responde_ao_assistente and not aciona:
        instrucao = instruction_of(texto, self_chat=True)
        aciona = bool(instrucao)

    resumo.update({
        "reply_to": clean({"id": citada["id"], "text": citada.get("text")}) if citada and aciona else None,
        "message_id": key.get("id"),
        "from_me": minha,
        "chat": jid_to_number(jid) if jid else None,
        "chat_jid": jid,
        "chat_type": ("grupo" if str(jid).endswith("@g.us") else "direto") if jid else None,
        "self_chat": True if propria else None,
        "type": tipo,
        "preview": (texto[:PREVIEW_CHARS] + "…") if texto and len(texto) > PREVIEW_CHARS else texto,
        "instruction": instrucao if aciona else None,
        "trigger": True if aciona else None,
    })
    return clean(resumo)


class EventLog:
    """Últimos eventos recebidos, em memória.

    Guarda também quais acionamentos já foram tratados, para que uma sessão em
    laço possa perguntar "o que sobrou?" sem reprocessar nem varrer conversas.
    """

    def __init__(self, maxlen: int = MAX_EVENTS, store: Any = None, owner_number: str | None = None):
        self._eventos: deque[dict[str, Any]] = deque(maxlen=maxlen)
        self.store = store if store is not None else build_store()
        self.owner_number = owner_number if owner_number is not None else os.environ.get("EVOLUTION_OWNER_NUMBER", "")
        self.total = 0
        self.started = datetime.now()

    def pending(self, limit: int = 10) -> list[dict[str, Any]]:
        """Acionamentos ainda não tratados, do mais antigo para o mais novo."""
        acionamentos = [e for e in self._eventos if e.get("trigger") and e.get("message_id")]
        if not acionamentos:
            return []
        tratados = self.store.handled_among(e["message_id"] for e in acionamentos)
        return [e for e in acionamentos if e["message_id"] not in tratados][:limit]

    def trigger(self, message_id: str | None) -> dict[str, Any] | None:
        """O resumo de um acionamento conhecido, ou None. Só acionamentos: uma
        mensagem enviada por nós nunca é devolvida aqui, então nunca é marcada."""
        if not message_id:
            return None
        for e in self._eventos:
            if e.get("trigger") and e.get("message_id") == message_id:
                return e
        return None

    def is_handled(self, message_id: str) -> bool:
        """True se este acionamento já foi tratado (consulta o armazenamento)."""
        return bool(message_id) and self.store.is_handled(message_id)

    def mark_handled(self, message_ids: list[str], chat: str | None = None,
                     instruction: str | None = None) -> int:
        """Marca acionamentos como tratados. Devolve quantos passaram a contar."""
        detalhes = {
            e["message_id"]: e for e in self._eventos
            if e.get("message_id") and e.get("trigger")
        }
        novos = 0
        for mid in message_ids or []:
            if not mid:
                continue
            e = detalhes.get(mid, {})
            if self.store.mark(mid, chat or e.get("chat"), instruction or e.get("preview")):
                novos += 1
        return novos

    def add(self, payload: Any) -> dict[str, Any]:
        resumo = summarize_event(payload, owner_number=self.owner_number, replied_to_us=self.store.is_handled)
        self._eventos.append(resumo)
        self.total += 1
        marca = " <<< ACIONAMENTO" if resumo.get("trigger") else ""
        if resumo.get("self_chat"):
            marca += " (conversa pessoal)"
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
            "pendentes": len(self.pending(limit=999)),
            "armazenamento": self.store.describe(),
            "dono_configurado": bool(self.owner_number),
            "eventos": eventos[-limit:],
        }


# Instância única do processo: o receptor HTTP escreve aqui e as tools do MCP leem.
# Os dois rodam no mesmo processo (mcp_http serve as duas coisas).
EVENTS = EventLog()
