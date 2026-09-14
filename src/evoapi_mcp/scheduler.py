"""Mensagens agendadas: "manda isto para fulano amanhã às 9h".

O executor que trata as instruções é uma sessão nova a cada acionamento, sem
relógio nem memória entre elas. Quem consegue lembrar de mandar algo daqui a um
dia é o servidor, que fica de pé o tempo todo e já tem um Postgres. Então:

- a tool `schedule_message` grava o pedido no banco (tabela `scheduled_messages`);
- esta thread olha a cada `INTERVALO_S` o que venceu e envia, em texto ou em voz;
- o id da mensagem enviada é marcado como tratado, porque na conversa pessoal
  toda mensagem "minha" é instrução e o envio voltaria como pedido.

O horário é guardado com fuso. O que Max escreve ("amanhã às 9h") é interpretado
pelo executor com a data atual que o script lhe dá; aqui só chega um instante
absoluto, ou um texto ISO que `parse_when` completa com o fuso configurado.
"""

from __future__ import annotations

import sys
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

INTERVALO_S = 30
MAX_TEXT = 4000
MARCADOR = "[agendada]"


class ScheduleError(Exception):
    """Pedido de agendamento inválido."""


def _log(message: str, level: str = "INFO") -> None:
    print(f"[{level}] Agenda: {message}", file=sys.stderr)


def parse_when(when: str, tz: str, now: datetime | None = None) -> datetime:
    """Converte o texto do horário num instante com fuso.

    Aceita "2026-09-15 09:00", "2026-09-15T09:00", com ou sem segundos, com ou sem
    deslocamento. Sem deslocamento, vale o fuso `tz`. Também aceita só "09:00":
    hoje, ou amanhã se já passou. Recusa o passado.
    """
    texto = (when or "").strip()
    if not texto:
        raise ScheduleError("horário vazio")
    zona = ZoneInfo(tz)
    agora = (now or datetime.now(timezone.utc)).astimezone(zona)

    if len(texto) <= 5 and ":" in texto:  # "9:00" / "09:00"
        try:
            hora, minuto = (int(p) for p in texto.split(":", 1))
            alvo = agora.replace(hour=hora, minute=minuto, second=0, microsecond=0)
        except ValueError:
            raise ScheduleError(f"horário inválido: {texto!r}") from None
        if alvo <= agora:
            alvo += timedelta(days=1)
        return alvo

    try:
        alvo = datetime.fromisoformat(texto.replace(" ", "T", 1))
    except ValueError:
        raise ScheduleError(
            f"horário inválido: {texto!r}; use 'AAAA-MM-DD HH:MM' ou só 'HH:MM'"
        ) from None
    if alvo.tzinfo is None:
        alvo = alvo.replace(tzinfo=zona)
    if alvo <= agora - timedelta(minutes=1):
        raise ScheduleError(f"{alvo.astimezone(zona).strftime('%d/%m/%Y %H:%M')} já passou")
    return alvo


def fmt(dt: datetime | None, tz: str) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(ZoneInfo(tz)).strftime("%Y-%m-%d %H:%M")


class Scheduler:
    """Envia o que venceu. Nunca derruba o processo."""

    def __init__(
        self,
        store: Any,
        send_text: Callable[[str, str], Any],
        send_voice: Callable[[str, str], Any] | None = None,
        tz: str = "America/Fortaleza",
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ):
        self.store = store
        self.send_text = send_text
        self.send_voice = send_voice
        self.tz = tz
        self.clock = clock
        self.sent = 0
        self.failed = 0

    # ---------------------------------------------------------------- pedidos

    def schedule(self, chat: str, text: str, when: str, voice: bool = False) -> dict[str, Any]:
        texto = (text or "").strip()
        if not texto:
            raise ScheduleError("texto vazio")
        if len(texto) > MAX_TEXT:
            raise ScheduleError(f"texto com {len(texto)} caracteres; o máximo é {MAX_TEXT}")
        if voice and self.send_voice is None:
            raise ScheduleError("voz indisponível neste servidor")
        chat_limpo = (chat or "").strip()
        if not chat_limpo:
            raise ScheduleError("destinatário vazio")
        alvo = parse_when(when, self.tz, now=self.clock())
        kind = "voice" if voice else "text"
        sid = self.store.schedule(chat_limpo, texto, alvo, kind)
        _log(f"#{sid} para {chat_limpo} em {fmt(alvo, self.tz)} ({kind})")
        return self._item({"id": sid, "chat": chat_limpo, "text": texto, "kind": kind, "send_at": alvo, "status": "pending"})

    def pending(self, limit: int = 50) -> list[dict[str, Any]]:
        return [self._item(i) for i in self.store.pending_scheduled(limit=limit)]

    def cancel(self, sid: int) -> bool:
        ok = self.store.cancel_scheduled(int(sid))
        if ok:
            _log(f"#{sid} cancelada")
        return ok

    # ---------------------------------------------------------------- envio

    def check(self) -> list[dict[str, Any]]:
        """Uma passada: envia o que venceu. Devolve o que foi processado."""
        agora = self.clock()
        processadas = []
        for item in self.store.due(agora):
            sid = item["id"]
            try:
                if item.get("kind") == "voice":
                    if self.send_voice is None:
                        raise ScheduleError("voz indisponível")
                    resultado = self.send_voice(item["chat"], item["text"])
                else:
                    resultado = self.send_text(item["chat"], item["text"])
                message_id = ((resultado or {}).get("key") or {}).get("id") if isinstance(resultado, dict) else None
                self.store.mark_scheduled(sid, "sent", message_id=message_id)
                if message_id:
                    # Na conversa pessoal isto voltaria como instrução do dono.
                    self.store.mark(message_id, chat=item["chat"], instruction=f"{MARCADOR} #{sid}")
                self.sent += 1
                _log(f"#{sid} enviada para {item['chat']}")
                processadas.append({"id": sid, "status": "sent"})
            except Exception as e:
                self.store.mark_scheduled(sid, "failed", error=str(e)[:500])
                self.failed += 1
                _log(f"#{sid} falhou: {e}", "ERROR")
                processadas.append({"id": sid, "status": "failed", "error": str(e)})
        return processadas

    def run_forever(self, interval_s: float = INTERVALO_S, stop: threading.Event | None = None) -> None:
        stop = stop or threading.Event()
        _log(f"ativa: verifica a cada {int(interval_s)} s, fuso {self.tz}")
        while not stop.is_set():
            try:
                self.check()
            except Exception as e:
                _log(f"erro na verificação: {e}", "ERROR")
            stop.wait(interval_s)

    def start(self, interval_s: float = INTERVALO_S) -> threading.Thread:
        t = threading.Thread(target=self.run_forever, args=(interval_s,), name="agenda", daemon=True)
        t.start()
        return t

    # ---------------------------------------------------------------- saída

    def _item(self, i: dict[str, Any]) -> dict[str, Any]:
        texto = i.get("text") or ""
        return {
            "id": i.get("id"),
            "chat": i.get("chat"),
            "quando": fmt(i.get("send_at"), self.tz),
            "tipo": "voz" if i.get("kind") == "voice" else "texto",
            "texto": texto if len(texto) <= 120 else texto[:117] + "...",
            "status": i.get("status"),
        }

    def describe(self) -> dict[str, Any]:
        return {
            "ativa": True,
            "fuso": self.tz,
            "pendentes": len(self.store.pending_scheduled(limit=999)),
            "enviadas": self.sent,
            "falhas": self.failed,
        }
