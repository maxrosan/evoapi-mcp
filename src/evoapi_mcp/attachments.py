"""Pergunta o que fazer com arquivos que Max manda sozinhos na conversa pessoal.

Na conversa pessoal, só vira instrução o que tem texto ou fala. Um PDF sem legenda
não tem nenhum dos dois: não entrava na fila e ficava lá, sem ninguém perguntar.

O cuidado é não perguntar cedo demais. Max costuma mandar o arquivo e escrever a
instrução logo depois. Então:

1. arquivo sem legenda chega: fica aguardando;
2. se vier instrução dele (texto ou áudio) depois do arquivo, ou se o arquivo for
   entregue ao executor como anexo recente de alguma pendência, não há pergunta:
   o arquivo já é contexto;
3. se nada disso acontecer em `delay_s`, contado a partir do último arquivo do
   lote, o próprio servidor pergunta, sem acordar o Claude;
4. a próxima instrução de Max na conversa pessoal, citando a pergunta ou não, chega
   ao executor com a pergunta e os arquivos em questão.

Só a conversa pessoal: arquivos em grupos ou de terceiros nunca geram pergunta.
"""

from __future__ import annotations

import sys
import threading
import time
from typing import Any, Callable

MEDIA_KINDS = ("image", "video", "document")
ASK_DELAY_S = 60
QUESTION_TTL_S = 1800
# Citando a pergunta, a resposta ainda vale depois do prazo normal.
QUOTE_TTL_S = 86400
INTERVALO_S = 5
MARCADOR = "📎"


def _log(message: str, level: str = "INFO") -> None:
    print(f"[{level}] Anexos: {message}", file=sys.stderr)


def _descrever(arquivo: dict[str, Any]) -> str:
    if arquivo.get("arquivo"):
        return str(arquivo["arquivo"])
    return {"image": "uma imagem", "video": "um vídeo"}.get(arquivo.get("tipo"), "um arquivo")


def question_text(arquivos: list[dict[str, Any]]) -> str:
    opcoes = "Por exemplo: arquivar no Drive, indexar para achar depois, resumir ou colocar no Trello. É só responder aqui."
    if len(arquivos) == 1:
        return f"{MARCADOR} Recebi {_descrever(arquivos[0])}. O que faço com isso? {opcoes}"
    nomes = [_descrever(a) for a in arquivos[:3]]
    resto = len(arquivos) - len(nomes)
    lista = ", ".join(nomes) + (f" e mais {resto}" if resto > 0 else "")
    return f"{MARCADOR} Recebi {len(arquivos)} arquivos: {lista}. O que faço com eles? {opcoes}"


class AttachmentAsker:
    def __init__(
        self,
        events: Any,
        send: Callable[[str, str], Any],
        owner_number: str,
        delay_s: float = ASK_DELAY_S,
        question_ttl_s: float = QUESTION_TTL_S,
        clock: Callable[[], float] = time.time,
    ):
        self.events = events
        self.send = send
        self.owner_number = owner_number
        self.delay_s = float(delay_s)
        self.question_ttl_s = float(question_ttl_s)
        self.clock = clock
        self._aguardando: list[dict[str, Any]] = []
        self._perguntas: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self.enviadas = 0

    # ---------------------------------------------------------------- entrada

    def observe(self, event: dict[str, Any]) -> None:
        """Chamado para todo evento que chega: separa os arquivos soltos de Max."""
        if not (event.get("from_me") and event.get("self_chat")):
            return
        if event.get("trigger") or event.get("type") not in MEDIA_KINDS:
            return
        mid = event.get("message_id")
        if not mid or self.events.is_sent_by_assistant(mid):
            return
        with self._lock:
            if any(a["id"] == mid for a in self._aguardando):
                return
            self._aguardando.append({
                "id": mid, "tipo": event.get("type"), "arquivo": event.get("file"),
                "mime": event.get("mime"), "ts": float(event.get("ts") or self.clock()),
            })

    def consume(self, message_ids: list[str]) -> int:
        """Arquivos que já chegaram ao executor como contexto: sem pergunta."""
        ids = {m for m in message_ids or [] if m}
        with self._lock:
            antes = len(self._aguardando)
            self._aguardando = [a for a in self._aguardando if a["id"] not in ids]
            return antes - len(self._aguardando)

    # ---------------------------------------------------------------- pergunta

    def check(self) -> dict[str, Any] | None:
        agora = self.clock()
        with self._lock:
            self._perguntas = [q for q in self._perguntas if agora - q["ts"] <= QUOTE_TTL_S]
            if not self._aguardando:
                return None
            if agora - max(a["ts"] for a in self._aguardando) < self.delay_s:
                return None
            lote = list(self._aguardando)
        ids = {a["id"] for a in lote}

        primeiro = min(a["ts"] for a in lote)
        if self.events.owner_instruction_since(primeiro):
            self.consume(list(ids))
            _log(f"{len(lote)} arquivo(s) viraram contexto de uma instrução; sem pergunta")
            return None

        texto = question_text(lote)
        try:
            resultado = self.send(self.owner_number, texto)
        except Exception as e:
            self.consume(list(ids))
            _log(f"falha ao perguntar sobre {len(lote)} arquivo(s): {e}", "ERROR")
            return None

        qid = ((resultado or {}).get("key") or {}).get("id") if isinstance(resultado, dict) else None
        if qid:
            # A pergunta volta pelo webhook como mensagem do dono: não pode virar instrução.
            self.events.note_sent(qid)
            try:
                self.events.store.mark(qid, chat=self.owner_number, instruction=f"{MARCADOR} pergunta sobre anexo")
            except Exception as e:
                _log(f"não consegui marcar a pergunta: {e}", "WARNING")
        pergunta = {
            "id": qid, "texto": texto, "ts": agora,
            "arquivos": [{k: a[k] for k in ("id", "tipo", "arquivo", "mime") if a.get(k)} for a in lote],
        }
        with self._lock:
            self._aguardando = [a for a in self._aguardando if a["id"] not in ids]
            self._perguntas.append(pergunta)
            self.enviadas += 1
        _log(f"perguntei sobre {len(lote)} arquivo(s)")
        return pergunta

    # ---------------------------------------------------------------- resposta

    def on_trigger(self, event: dict[str, Any]) -> dict[str, Any] | None:
        """Liga uma instrução de Max à pergunta que ela responde, se houver."""
        if not event.get("self_chat"):
            return None
        ts = float(event.get("ts") or self.clock())
        citada = (event.get("reply_to") or {}).get("id")
        with self._lock:
            alvo = next((q for q in self._perguntas if citada and q["id"] == citada), None)
            por_citacao = alvo is not None
            if alvo is None:
                abertas = [q for q in self._perguntas if q["ts"] <= ts and ts - q["ts"] <= self.question_ttl_s]
                alvo = max(abertas, key=lambda q: q["ts"]) if abertas else None
            if alvo is None:
                return None
            self._perguntas.remove(alvo)
        event["question"] = {"id": alvo["id"], "text": alvo["texto"]}
        event["files_in_question"] = alvo["arquivos"]
        if por_citacao:
            # A citada é a pergunta, não um anexo: o executor deve usar files_in_question.
            event["reply_to"] = None
        return alvo

    # ---------------------------------------------------------------- thread

    def run_forever(self, interval_s: float = INTERVALO_S, stop: threading.Event | None = None) -> None:
        stop = stop or threading.Event()
        _log(f"ativo: pergunta {int(self.delay_s)} s depois de um arquivo sem instrução")
        while not stop.is_set():
            try:
                self.check()
            except Exception as e:
                _log(f"erro na verificação: {e}", "ERROR")
            stop.wait(interval_s)

    def start(self, interval_s: float = INTERVALO_S) -> threading.Thread:
        t = threading.Thread(target=self.run_forever, args=(interval_s,), name="anexos", daemon=True)
        t.start()
        return t

    def describe(self) -> dict[str, Any]:
        agora = self.clock()
        with self._lock:
            abertas = sum(1 for q in self._perguntas if agora - q["ts"] <= self.question_ttl_s)
            return {
                "ativo": True, "espera_s": int(self.delay_s), "arquivos_aguardando": len(self._aguardando),
                "perguntas_abertas": abertas, "perguntas_enviadas": self.enviadas,
            }
