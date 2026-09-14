"""Histórico de pedidos e respostas, gravado pelo servidor sem gastar tokens do Claude.

O servidor vê as duas pontas da conversa com o assistente:
- o pedido chega pelo webhook e vira acionamento (`open`);
- toda resposta sai pelas ferramentas de envio (`response`), que conhecem o texto;
- quando a pendência é marcada como tratada (`close`), pedido e respostas viram um
  registro só, com vetor, na fila rápida.

Assim, meses depois, "o que eu pedi sobre o NARA?" acha a troca inteira. Mensagens
de terceiros não entram: só instruções de Max e o que o assistente respondeu a elas.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

from evoapi_mcp.jobs import RAPIDA
from evoapi_mcp.vectors import VectorSet

# Uma resposta só é ligada a um pedido aberto há menos que isto.
OPEN_TTL_S = 3600
MAX_TEXT = 4000
MAX_RESULTS = 20


class HistoryError(Exception):
    """Consulta inválida ao histórico."""


def _texto(pedido: str | None, resposta: str | None) -> str:
    pedido = (pedido or "").strip()
    resposta = (resposta or "").strip()
    return f"Pedido: {pedido}\nResposta: {resposta}" if resposta else f"Pedido: {pedido}"


def _corta(texto: str | None, n: int) -> str | None:
    if not texto:
        return None
    return texto if len(texto) <= n else texto[: n - 3] + "..."


class ConversationLog:
    def __init__(self, store: Any, embedder: Any, queues: Any, owner_number: str = "",
                 tz: str = "America/Fortaleza", clock: Callable[[], float] = time.time):
        self.store = store
        self.embedder = embedder
        self.queues = queues
        self.owner_number = owner_number
        self.tz = tz
        self.clock = clock
        self.vectors = VectorSet()
        self._carregado = False
        self._abertas: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._lock_carga = threading.Lock()

    # ---------------------------------------------------------------- chaves de conversa

    def _chaves(self, jid: str | None, alt: str | None = None) -> set[str]:
        """Formas de reconhecer a mesma conversa: o pedido pode chegar por @lid e a
        resposta sair pelo número. Compara jid exato e os últimos dígitos."""
        from evoapi_mcp.webhook import _tail, is_self_chat

        chaves: set[str] = set()
        if is_self_chat(jid, alt, self.owner_number):
            chaves.add("pessoal")
        for valor in (jid, alt):
            if not valor:
                continue
            usuario, _, servidor = str(valor).partition("@")
            usuario = usuario.split(":")[0]
            chaves.add(f"{usuario}@{servidor}" if servidor else usuario)
            final = _tail(valor)
            if final and not str(valor).endswith("@g.us") and not str(valor).endswith("@lid"):
                chaves.add(f"#{final}")
        return chaves

    # ---------------------------------------------------------------- ciclo de uma troca

    def open(self, event: dict[str, Any]) -> int | None:
        """Registra um pedido de Max assim que ele vira acionamento."""
        tid = event.get("message_id")
        pedido = (event.get("instruction") or "").strip()
        if not tid or not pedido:
            return None
        with self._lock:
            if tid in self._abertas:
                return self._abertas[tid]["id"]
        citada = event.get("reply_to") or {}
        citacao = citada.get("text") or citada.get("file")
        chat = event.get("chat_jid") or event.get("chat")
        cid = self.store.add_conversation(tid, chat, pedido[:MAX_TEXT], bool(event.get("voice")), citacao)
        if cid is None:
            return None
        chaves = self._chaves(event.get("chat_jid"), event.get("chat_alt"))
        if event.get("self_chat"):
            chaves.add("pessoal")
        with self._lock:
            self._abertas[tid] = {
                "id": cid, "chat": chat, "chaves": chaves, "ts": self.clock(),
                "pedido": pedido, "respostas": [],
            }
        return cid

    def response(self, jid: str | None, alt: str | None, text: str | None) -> int | None:
        """Liga uma resposta enviada ao pedido aberto mais recente da mesma conversa."""
        texto = (text or "").strip()
        if not texto:
            return None
        chaves = self._chaves(jid, alt)
        agora = self.clock()
        with self._lock:
            candidatos = [
                c for c in self._abertas.values()
                if c["chaves"] & chaves and agora - c["ts"] <= OPEN_TTL_S
            ]
            if not candidatos:
                return None
            alvo = max(candidatos, key=lambda c: c["ts"])
            alvo["respostas"].append(texto)
        self.store.append_conversation_response(alvo["id"], texto[:MAX_TEXT])
        return alvo["id"]

    def close(self, trigger_id: str) -> int | None:
        """Pendência tratada: gera o vetor da troca na fila rápida."""
        with self._lock:
            conversa = self._abertas.pop(trigger_id, None)
        if conversa is None:
            # Servidor reiniciou entre o pedido e o fim: recupera do banco.
            linha = self.store.get_conversation_by_trigger(trigger_id)
            if linha is None or linha.get("embedding") is not None:
                return None
            conversa = {
                "id": linha["id"], "chat": linha.get("chat"), "pedido": linha.get("request"),
                "respostas": [linha["response"]] if linha.get("response") else [],
            }
        self.queues.submit(RAPIDA, self._indexar, conversa)
        return conversa["id"]

    def _indexar(self, conversa: dict[str, Any]) -> None:
        resposta = "\n".join(conversa["respostas"])
        texto = _texto(conversa["pedido"], resposta)
        vetor = self.embedder.embed([texto])[0]
        self.store.finish_conversation(conversa["id"], [float(x) for x in vetor])
        if self._carregado:
            self.vectors.remove_where(lambda item: item.get("id") == conversa["id"])
            self.vectors.add({
                "id": conversa["id"], "chat": conversa.get("chat"), "request": conversa["pedido"],
                "response": resposta or None, "created_at": datetime.now(timezone.utc),
            }, vetor, texto)

    # ---------------------------------------------------------------- busca

    def _carregar(self) -> None:
        with self._lock_carga:
            if not self._carregado:
                self.vectors.replace(
                    self.store.load_conversations(),
                    text_of=lambda item: _texto(item.get("request"), item.get("response")),
                )
                self._carregado = True

    def search(self, query: str, limit: int = 5, chat: str | None = None) -> list[dict[str, Any]]:
        consulta = (query or "").strip()
        if not consulta:
            raise HistoryError("consulta vazia")
        self._carregar()
        if not len(self.vectors):
            return []
        vetor = self.embedder.embed([consulta])[0]
        filtro = (lambda item: chat in (item.get("chat") or "")) if chat else None
        n = max(1, min(int(limit or 5), MAX_RESULTS))
        return [self._saida(item, score) for score, item in self.vectors.rank(vetor, consulta, n, filtro)]

    def _saida(self, item: dict[str, Any], score: float) -> dict[str, Any]:
        quando = item.get("created_at")
        out = {
            "id": item.get("id"),
            "quando": quando.astimezone(ZoneInfo(self.tz)).strftime("%Y-%m-%d %H:%M") if quando else None,
            "chat": item.get("chat"),
            "pedido": _corta(item.get("request"), 300),
            "resposta": _corta(item.get("response"), 400),
            "score": round(score, 2),
        }
        return {k: v for k, v in out.items() if v is not None}

    def describe(self) -> dict[str, Any]:
        with self._lock:
            abertas = len(self._abertas)
        return {"ativo": True, "conversas": self.store.count_conversations(), "abertas": abertas}
