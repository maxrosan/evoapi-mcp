"""Vigia da fila: avisa Max no WhatsApp quando o laço parou de consumir.

O que motivou isto: o servidor ficou nove horas enfileirando instruções que
ninguém consumia, porque a sessão que roda o laço morreu depois de um redeploy
sem que nada na tela dela dissesse isso. A falha era invisível por construção:
o servidor não tem como saber se o laço está vivo, só se a fila anda.

Então a regra é a única que o servidor consegue verificar sozinho: **se uma
instrução ficou tempo demais na fila sem ser tratada, alguém deveria ter agido e
não agiu**. Nesse caso ele manda uma mensagem na conversa pessoal do dono, uma
vez, e só repete depois de um intervalo de silêncio, para não virar spam.

O aviso é uma mensagem `fromMe` na conversa pessoal, ou seja, seria ele mesmo
uma instrução. Duas defesas contra o laço: o id do aviso é marcado como tratado
assim que o envio devolve, e o vigia ignora pendências que começam com o seu
próprio marcador, caso a marcação falhe.
"""

from __future__ import annotations

import sys
import threading
import time
from typing import Any, Callable

MARCADOR = "🛡️ Vigia:"
INTERVALO_PADRAO_S = 60


def _log(message: str, level: str = "INFO") -> None:
    print(f"[{level}] Vigia: {message}", file=sys.stderr)


def _minutos(segundos: float) -> int:
    return max(1, int(segundos // 60))


class Watchdog:
    """Observa a fila e avisa quando ela para de andar."""

    def __init__(
        self,
        events: Any,
        send: Callable[[str, str], Any],
        owner_number: str,
        max_age_s: float,
        cooldown_s: float,
        clock: Callable[[], float] = time.time,
    ):
        self.events = events
        self.send = send
        self.owner_number = owner_number
        self.max_age_s = float(max_age_s)
        self.cooldown_s = float(cooldown_s)
        self.clock = clock
        self.alerted_at: float | None = None
        self.alerts = 0

    # ---------------------------------------------------------------- lógica

    def stale(self) -> list[dict[str, Any]]:
        """Pendências velhas demais, ignorando os próprios avisos."""
        agora = self.clock()
        velhas = []
        for e in self.events.pending(limit=999):
            instrucao = str(e.get("instruction") or "")
            if instrucao.startswith(MARCADOR):
                continue
            ts = e.get("ts")
            if ts is None:
                continue  # sem carimbo não dá para medir idade; não alarma
            if agora - float(ts) >= self.max_age_s:
                velhas.append(e)
        return velhas

    def check(self) -> dict[str, Any] | None:
        """Uma passada. Devolve o aviso enviado, ou None quando não há o que fazer."""
        velhas = self.stale()
        if not velhas:
            # Fila voltou a andar (ou nunca parou): próximo travamento alarma de novo.
            self.alerted_at = None
            return None

        agora = self.clock()
        if self.alerted_at is not None and agora - self.alerted_at < self.cooldown_s:
            return None

        mais_antiga = min(velhas, key=lambda e: float(e.get("ts", agora)))
        idade = _minutos(agora - float(mais_antiga.get("ts", agora)))
        previa = str(mais_antiga.get("instruction") or "")[:60]
        chat = mais_antiga.get("chat") or "?"
        n = len(velhas)
        substantivo = "instruções paradas" if n > 1 else "instrução parada"
        texto = (
            f"{MARCADOR} {n} {substantivo} "
            f"há {idade} min sem ninguém responder. O laço parece ter parado. "
            f"Mais antiga: \"{previa}\" ({chat})."
        )

        try:
            resultado = self.send(self.owner_number, texto)
        except Exception as e:  # o vigia nunca pode derrubar o processo
            _log(f"falha ao enviar aviso: {e}", "ERROR")
            return None

        # O aviso é uma mensagem do dono na conversa dele: sem isto vira instrução.
        try:
            enviado_id = ((resultado or {}).get("key") or {}).get("id")
            if enviado_id:
                self.events.store.mark(enviado_id, chat=self.owner_number, instruction=f"{MARCADOR} aviso")
                if hasattr(self.events, "note_sent"):
                    self.events.note_sent(enviado_id)
        except Exception as e:
            _log(f"não consegui marcar o próprio aviso: {e}", "WARNING")

        self.alerted_at = agora
        self.alerts += 1
        _log(f"aviso enviado: {n} pendência(s), mais antiga há {idade} min")
        return {"pendentes": n, "mais_antiga_min": idade, "texto": texto}

    # ---------------------------------------------------------------- thread

    def run_forever(self, interval_s: float = INTERVALO_PADRAO_S, stop: threading.Event | None = None) -> None:
        stop = stop or threading.Event()
        _log(f"ativo: avisa após {_minutos(self.max_age_s)} min parado, repete a cada {_minutos(self.cooldown_s)} min")
        while not stop.is_set():
            try:
                self.check()
            except Exception as e:  # idem: nunca morrer calado
                _log(f"erro na verificação: {e}", "ERROR")
            stop.wait(interval_s)

    def start(self, interval_s: float = INTERVALO_PADRAO_S) -> threading.Thread:
        t = threading.Thread(target=self.run_forever, args=(interval_s,), name="vigia", daemon=True)
        t.start()
        return t

    def describe(self) -> dict[str, Any]:
        return {
            "ativo": True,
            "avisa_apos_min": _minutos(self.max_age_s),
            "repete_a_cada_min": _minutos(self.cooldown_s),
            "avisos_enviados": self.alerts,
        }
