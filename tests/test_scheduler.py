"""Mensagens agendadas: interpretação do horário, guarda no armazenamento e envio."""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from evoapi_mcp.scheduler import MARCADOR, ScheduleError, Scheduler, fmt, parse_when
from evoapi_mcp.store import MemoryStore

TZ = "America/Fortaleza"
AGORA = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)  # 09:00 em Fortaleza


class Relogio:
    def __init__(self, t=AGORA):
        self.t = t

    def __call__(self):
        return self.t

    def avanca(self, **kw):
        self.t += timedelta(**kw)


@pytest.fixture
def cenario():
    relogio = Relogio()
    enviados = []

    def send_text(chat, texto):
        enviados.append(("text", chat, texto))
        return {"key": {"id": f"T{len(enviados)}"}}

    def send_voice(chat, texto):
        enviados.append(("voice", chat, texto))
        return {"key": {"id": f"V{len(enviados)}"}}

    store = MemoryStore()
    agenda = Scheduler(store, send_text, send_voice, tz=TZ, clock=relogio)
    return agenda, store, enviados, relogio


# ---------------------------------------------------------------- parse_when

def test_data_e_hora_sem_fuso_usa_o_fuso_de_max():
    alvo = parse_when("2026-09-15 09:00", TZ, now=AGORA)
    assert alvo.utcoffset() == timedelta(hours=-3)
    assert alvo.astimezone(timezone.utc) == datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)


def test_formato_iso_com_t_e_com_deslocamento():
    assert parse_when("2026-09-15T09:00", TZ, now=AGORA).hour == 9
    com_offset = parse_when("2026-09-15T09:00-03:00", TZ, now=AGORA)
    assert com_offset.astimezone(timezone.utc).hour == 12


def test_so_hora_e_hoje_se_ainda_nao_passou():
    alvo = parse_when("18:30", TZ, now=AGORA)
    assert alvo.astimezone(ZoneInfo(TZ)).strftime("%Y-%m-%d %H:%M") == "2026-09-14 18:30"


def test_so_hora_vira_amanha_se_ja_passou():
    alvo = parse_when("08:00", TZ, now=AGORA)  # agora são 09:00
    assert alvo.astimezone(ZoneInfo(TZ)).strftime("%Y-%m-%d %H:%M") == "2026-09-15 08:00"


@pytest.mark.parametrize("texto", ["", "ontem", "2026-13-01 10:00", "25:99"])
def test_horario_invalido(texto):
    with pytest.raises(ScheduleError):
        parse_when(texto, TZ, now=AGORA)


def test_passado_e_recusado():
    with pytest.raises(ScheduleError, match="já passou"):
        parse_when("2026-09-14 08:00", TZ, now=AGORA)


def test_fmt_mostra_no_fuso_de_max():
    assert fmt(AGORA, TZ) == "2026-09-14 09:00"
    assert fmt(None, TZ) is None


# ---------------------------------------------------------------- agendar

def test_agendar_guarda_e_lista(cenario):
    agenda, store, enviados, _ = cenario
    item = agenda.schedule("558498140038", "Bom dia, Keilla!", "2026-09-15 09:00")
    assert item["id"] == 1
    assert item["quando"] == "2026-09-15 09:00"
    assert item["tipo"] == "texto"
    assert agenda.pending() == [item]
    assert enviados == []


def test_agendar_recusa_texto_vazio_e_destinatario_vazio(cenario):
    agenda, *_ = cenario
    with pytest.raises(ScheduleError, match="texto vazio"):
        agenda.schedule("558498140038", "  ", "2026-09-15 09:00")
    with pytest.raises(ScheduleError, match="destinatário"):
        agenda.schedule("", "oi", "2026-09-15 09:00")


def test_agendar_voz_sem_backend_recusa():
    agenda = Scheduler(MemoryStore(), lambda c, t: {}, None, tz=TZ, clock=Relogio())
    with pytest.raises(ScheduleError, match="voz"):
        agenda.schedule("558498140038", "oi", "2026-09-15 09:00", voice=True)


def test_cancelar(cenario):
    agenda, store, enviados, relogio = cenario
    item = agenda.schedule("558498140038", "oi", "2026-09-15 09:00")
    assert agenda.cancel(item["id"]) is True
    assert agenda.cancel(item["id"]) is False   # já cancelada
    assert agenda.cancel(999) is False
    assert agenda.pending() == []
    relogio.avanca(days=2)
    assert agenda.check() == []
    assert enviados == []


# ---------------------------------------------------------------- envio

def test_nao_envia_antes_da_hora(cenario):
    agenda, store, enviados, relogio = cenario
    agenda.schedule("558498140038", "oi", "2026-09-15 09:00")
    relogio.avanca(hours=23)
    assert agenda.check() == []
    assert enviados == []


def test_envia_quando_vence_e_marca_como_tratada(cenario):
    agenda, store, enviados, relogio = cenario
    item = agenda.schedule("558498140038", "Bom dia, Keilla!", "2026-09-15 09:00")
    relogio.avanca(days=1)
    assert agenda.check() == [{"id": item["id"], "status": "sent"}]
    assert enviados == [("text", "558498140038", "Bom dia, Keilla!")]
    assert agenda.pending() == []
    assert store.is_handled("T1")                       # não volta como instrução
    assert agenda.check() == []                         # não reenvia
    assert agenda.describe()["enviadas"] == 1


def test_envia_em_voz(cenario):
    agenda, store, enviados, relogio = cenario
    agenda.schedule("558498140038", "Bom dia!", "2026-09-15 09:00", voice=True)
    relogio.avanca(days=1)
    agenda.check()
    assert enviados == [("voice", "558498140038", "Bom dia!")]
    assert store.is_handled("V1")


def test_envia_na_ordem_e_so_as_vencidas(cenario):
    agenda, store, enviados, relogio = cenario
    agenda.schedule("A", "segunda", "2026-09-15 10:00")
    agenda.schedule("B", "primeira", "2026-09-15 09:00")
    agenda.schedule("C", "futura", "2026-09-20 09:00")
    relogio.avanca(days=1, hours=2)
    agenda.check()
    assert [e[2] for e in enviados] == ["primeira", "segunda"]
    assert [i["texto"] for i in agenda.pending()] == ["futura"]


def test_falha_no_envio_marca_e_nao_repete(cenario):
    agenda, store, enviados, relogio = cenario

    def explode(chat, texto):
        raise RuntimeError("Evolution fora")

    agenda.send_text = explode
    agenda.schedule("558498140038", "oi", "2026-09-15 09:00")
    relogio.avanca(days=1)
    res = agenda.check()
    assert res[0]["status"] == "failed"
    assert "Evolution fora" in res[0]["error"]
    assert agenda.pending() == []
    assert agenda.check() == []
    assert agenda.describe()["falhas"] == 1


def test_marca_tratada_com_o_marcador(cenario):
    agenda, store, enviados, relogio = cenario
    agenda.schedule("5584999290327", "lembrete", "2026-09-15 09:00")
    relogio.avanca(days=1)
    agenda.check()
    assert store.is_handled("T1")
    assert MARCADOR  # existe e é usado na instrução gravada


def test_describe(cenario):
    agenda, *_ = cenario
    agenda.schedule("A", "x", "2026-09-15 09:00")
    assert agenda.describe() == {"ativa": True, "fuso": TZ, "pendentes": 1, "enviadas": 0, "falhas": 0}
