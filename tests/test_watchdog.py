"""Vigia da fila: avisa quando o laço parou de consumir, sem virar spam nem laço."""

import pytest

from evoapi_mcp.store import MemoryStore
from evoapi_mcp.watchdog import MARCADOR, Watchdog
from evoapi_mcp.webhook import EventLog, summarize_event

DONO = "5584999290327"
TS_MSG = 1789170336


def evento(texto, msg_id, jid="120363@g.us"):
    return {
        "event": "messages.upsert",
        "data": {
            "key": {"id": msg_id, "fromMe": True, "remoteJid": jid},
            "messageType": "conversation",
            "message": {"conversation": texto},
            "messageTimestamp": TS_MSG,
        },
    }


class Relogio:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def avanca(self, s):
        self.t += s


@pytest.fixture
def cenario(monkeypatch):
    """EventLog isolado, relógio controlado e envio falso que registra as chamadas."""
    relogio = Relogio()
    monkeypatch.setattr("evoapi_mcp.webhook.time.time", relogio)   # carimbo dos eventos
    log = EventLog(store=MemoryStore(), owner_number=DONO)
    enviados = []

    def send(numero, texto):
        enviados.append({"numero": numero, "texto": texto})
        return {"key": {"id": f"AVISO{len(enviados)}", "remoteJid": f"{DONO}@s.whatsapp.net"}}

    vigia = Watchdog(events=log, send=send, owner_number=DONO,
                     max_age_s=600, cooldown_s=3600, clock=relogio)
    return log, vigia, enviados, relogio


def test_evento_tem_carimbo_numerico():
    r = summarize_event(evento("IA: oi", "M1"))
    assert isinstance(r["ts"], float)


def test_fila_vazia_nao_avisa(cenario):
    log, vigia, enviados, _ = cenario
    assert vigia.check() is None
    assert enviados == []


def test_pendencia_recente_nao_avisa(cenario):
    log, vigia, enviados, relogio = cenario
    log.add(evento("IA: resuma", "M1"))
    relogio.avanca(300)          # 5 min: abaixo do limite de 10
    assert vigia.check() is None
    assert enviados == []


def test_pendencia_velha_avisa_uma_vez(cenario):
    log, vigia, enviados, relogio = cenario
    log.add(evento("IA: resuma o combinado", "M1"))
    relogio.avanca(601)

    aviso = vigia.check()
    assert aviso["pendentes"] == 1
    assert aviso["mais_antiga_min"] == 10
    assert len(enviados) == 1
    assert enviados[0]["numero"] == DONO
    assert enviados[0]["texto"].startswith(MARCADOR)
    assert "resuma o combinado" in enviados[0]["texto"]
    assert "120363@g.us" in enviados[0]["texto"]

    # a pendência continua lá: o vigia avisa, não resolve
    assert len(log.pending()) == 1


def test_proprio_aviso_e_marcado_como_tratado(cenario):
    """O aviso é fromMe na conversa pessoal: sem marcar, viraria instrução."""
    log, vigia, enviados, relogio = cenario
    log.add(evento("IA: x", "M1"))
    relogio.avanca(601)
    vigia.check()
    assert log.store.is_handled("AVISO1")


def test_nao_repete_dentro_do_intervalo(cenario):
    log, vigia, enviados, relogio = cenario
    log.add(evento("IA: x", "M1"))
    relogio.avanca(601)
    vigia.check()
    relogio.avanca(1800)          # 30 min depois, ainda dentro da hora
    assert vigia.check() is None
    assert len(enviados) == 1


def test_repete_depois_do_intervalo(cenario):
    log, vigia, enviados, relogio = cenario
    log.add(evento("IA: x", "M1"))
    relogio.avanca(601)
    vigia.check()
    relogio.avanca(3601)
    assert vigia.check() is not None
    assert len(enviados) == 2


def test_fila_que_volta_a_andar_rearma(cenario):
    log, vigia, enviados, relogio = cenario
    log.add(evento("IA: x", "M1"))
    relogio.avanca(601)
    vigia.check()

    log.mark_handled(["M1"])      # o laço voltou e consumiu
    assert vigia.check() is None
    assert vigia.alerted_at is None   # rearmado

    log.add(evento("IA: y", "M2"))
    relogio.avanca(601)
    assert vigia.check() is not None  # nova parada alarma de novo, sem esperar a hora
    assert len(enviados) == 2


def test_conta_todas_as_velhas_e_cita_a_mais_antiga(cenario):
    log, vigia, enviados, relogio = cenario
    log.add(evento("IA: primeira", "M1"))
    relogio.avanca(700)
    log.add(evento("IA: segunda", "M2"))
    relogio.avanca(650)
    aviso = vigia.check()
    assert aviso["pendentes"] == 2
    assert "primeira" in enviados[0]["texto"]
    assert "2 instruções paradas" in enviados[0]["texto"]


def test_ignora_o_proprio_marcador_se_a_marcacao_falhar(cenario):
    """Defesa em profundidade: mesmo que o aviso caia na fila, o vigia não alarma sobre ele."""
    log, vigia, enviados, relogio = cenario
    log.add(evento(f"{MARCADOR} 1 instrução parada", "AVISO_ANTIGO", jid="110818863673433@lid"))
    relogio.avanca(601)
    assert vigia.check() is None
    assert enviados == []


def test_falha_no_envio_nao_derruba(cenario):
    log, vigia, enviados, relogio = cenario

    def explode(numero, texto):
        raise RuntimeError("Evolution fora do ar")

    vigia.send = explode
    log.add(evento("IA: x", "M1"))
    relogio.avanca(601)
    assert vigia.check() is None
    assert vigia.alerted_at is None   # tenta de novo na próxima passada


def test_evento_sem_carimbo_nao_alarma(cenario):
    log, vigia, enviados, relogio = cenario
    log.add(evento("IA: x", "M1"))
    for e in log._eventos:
        e.pop("ts", None)
    relogio.avanca(99999)
    assert vigia.check() is None


def test_describe(cenario):
    _, vigia, _, _ = cenario
    d = vigia.describe()
    assert d == {"ativo": True, "avisa_apos_min": 10, "repete_a_cada_min": 60, "avisos_enviados": 0}
