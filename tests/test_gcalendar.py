"""Google Agenda → WhatsApp: título, descrição, horário e sincronia com a fila de agendadas."""

from datetime import datetime, time, timedelta, timezone

import pytest

from evoapi_mcp.gcalendar import (
    CalendarError,
    CalendarWatcher,
    description_text,
    event_start,
    parse_title,
)
from evoapi_mcp.scheduler import Scheduler
from evoapi_mcp.store import MemoryStore

TZ = "America/Fortaleza"
AGORA = datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc)  # 09:00 em Fortaleza


class Relogio:
    def __init__(self, t=AGORA):
        self.t = t

    def __call__(self):
        return self.t

    def avanca(self, **kw):
        self.t += timedelta(**kw)


class AgendaFalsa:
    calendar_id = "primary"

    def __init__(self):
        self.eventos: dict[str, dict] = {}
        self.janelas = []

    def poe(self, eid, titulo, descricao, inicio_local, **extra):
        self.eventos[eid] = {
            "id": eid, "status": "confirmed", "summary": titulo, "description": descricao,
            "start": {"dateTime": inicio_local}, **extra,
        }

    def list_events(self, time_min, time_max):
        self.janelas.append((time_min, time_max))
        saida = []
        for ev in self.eventos.values():
            inicio = event_start(ev, TZ, time(9, 0))
            if time_min <= inicio <= time_max:
                saida.append(ev)
        return saida


@pytest.fixture
def cenario():
    relogio = Relogio()
    store = MemoryStore()
    agenda = AgendaFalsa()
    enviados = []

    def send_text(chat, texto):
        enviados.append((chat, texto))
        return {"key": {"id": f"M{len(enviados)}"}}

    fila = Scheduler(store, send_text, tz=TZ, clock=relogio)
    leitor = CalendarWatcher(store, agenda, tz=TZ, interval_s=3600, lookahead_s=24 * 3600, clock=relogio)
    return leitor, fila, agenda, store, enviados, relogio


# ---------------------------------------------------------------- título

@pytest.mark.parametrize("titulo, esperado", [
    ("Enviar no Whatsapp: 5584999990000", "5584999990000"),
    ("enviar no WhatsApp: +55 (84) 99999-0000", "5584999990000"),
    ("Enviar pelo Whats App 84 99999-0000", "5584999990000"),
    ("ENVIAR NO WHATSAPP:84999990000", "5584999990000"),
    ("Enviar no WhatsApp: 120363000000000000@g.us", "120363000000000000@g.us"),
])
def test_titulo_de_envio(titulo, esperado):
    assert parse_title(titulo) == esperado


@pytest.mark.parametrize("titulo", ["Reunião", "Lembrar de enviar no WhatsApp", "", "Enviar e-mail: x"])
def test_titulo_que_nao_e_de_envio(titulo):
    assert parse_title(titulo) is None


@pytest.mark.parametrize("titulo", ["Enviar no WhatsApp:", "Enviar no WhatsApp: Fulano", "Enviar no WhatsApp: 1234"])
def test_titulo_de_envio_com_numero_ruim(titulo):
    with pytest.raises(CalendarError):
        parse_title(titulo)


# ---------------------------------------------------------------- descrição e horário

def test_descricao_em_html_vira_texto_do_whatsapp():
    html = "Oi <b>Fulano</b>,<br>tudo bem?<br><br><br>Até &amp; logo<ul><li>um</li><li>dois</li></ul>"
    assert description_text(html) == "Oi *Fulano*,\ntudo bem?\n\nAté & logo\n• um\n• dois"


def test_descricao_em_texto_puro_fica_como_esta():
    assert description_text("  linha 1\nlinha 2  ") == "linha 1\nlinha 2"


def test_evento_de_dia_inteiro_sai_na_hora_configurada():
    inicio = event_start({"start": {"date": "2026-11-15"}}, TZ, time(9, 0))
    assert inicio.astimezone(timezone.utc) == datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)


def test_horario_com_z():
    inicio = event_start({"start": {"dateTime": "2026-11-15T12:00:00Z"}}, TZ, time(9, 0))
    assert inicio == datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------- sincronia

def test_evento_novo_vira_mensagem_e_sai_na_hora(cenario):
    leitor, fila, agenda, store, enviados, relogio = cenario
    agenda.poe("e1", "Enviar no WhatsApp: 84999990000", "Bom dia!", "2026-11-14T10:30:00-03:00")
    r = leitor.sync()
    assert [a["chat"] for a in r["agendados"]] == ["5584999990000"]
    assert r["agendados"][0]["quando"] == "2026-11-14 10:30"

    fila.check()
    assert enviados == []
    relogio.avanca(minutes=90)
    fila.check()
    assert enviados == [("5584999990000", "Bom dia!")]


def test_ler_de_novo_nao_duplica(cenario):
    leitor, fila, agenda, store, enviados, relogio = cenario
    agenda.poe("e1", "Enviar no WhatsApp: 84999990000", "Oi", "2026-11-14T10:30:00-03:00")
    leitor.sync()
    r = leitor.sync()
    assert r["agendados"] == [] and r["sem_mudanca"] == 1
    assert len(store.pending_scheduled()) == 1

    relogio.avanca(minutes=90)
    fila.check()
    leitor.sync()  # depois de enviado, continua sem reenviar
    fila.check()
    assert len(enviados) == 1


def test_evento_editado_troca_a_agendada(cenario):
    leitor, fila, agenda, store, enviados, relogio = cenario
    agenda.poe("e1", "Enviar no WhatsApp: 84999990000", "Oi", "2026-11-14T10:30:00-03:00")
    leitor.sync()
    agenda.poe("e1", "Enviar no WhatsApp: 84999990000", "Oi, mudou", "2026-11-14T11:00:00-03:00")
    r = leitor.sync()
    assert len(r["atualizados"]) == 1
    pendentes = store.pending_scheduled()
    assert len(pendentes) == 1 and pendentes[0]["text"] == "Oi, mudou"

    relogio.avanca(hours=3)
    fila.check()
    assert enviados == [("5584999990000", "Oi, mudou")]


def test_evento_apagado_cancela(cenario):
    leitor, fila, agenda, store, enviados, relogio = cenario
    agenda.poe("e1", "Enviar no WhatsApp: 84999990000", "Oi", "2026-11-14T10:30:00-03:00")
    leitor.sync()
    del agenda.eventos["e1"]
    r = leitor.sync()
    assert len(r["cancelados"]) == 1
    relogio.avanca(hours=3)
    fila.check()
    assert enviados == []


def test_titulo_que_deixou_de_ser_envio_cancela(cenario):
    leitor, fila, agenda, store, enviados, relogio = cenario
    agenda.poe("e1", "Enviar no WhatsApp: 84999990000", "Oi", "2026-11-14T10:30:00-03:00")
    leitor.sync()
    agenda.eventos["e1"]["summary"] = "Reunião"
    assert len(leitor.sync()["cancelados"]) == 1


def test_evento_sem_descricao_e_ignorado_com_motivo(cenario):
    leitor, fila, agenda, store, enviados, relogio = cenario
    agenda.poe("e1", "Enviar no WhatsApp: 84999990000", "", "2026-11-14T10:30:00-03:00")
    r = leitor.sync()
    assert r["agendados"] == []
    assert "descrição" in r["ignorados"][0]["motivo"]


def test_evento_criado_em_cima_da_hora_sai_atrasado(cenario):
    leitor, fila, agenda, store, enviados, relogio = cenario
    leitor.sync()  # 09:00: nada
    relogio.avanca(minutes=60)  # 10:00
    agenda.poe("e1", "Enviar no WhatsApp: 84999990000", "Atrasado", "2026-11-14T09:40:00-03:00")
    r = leitor.sync()
    assert r["agendados"][0].get("atrasado") is True
    fila.check()
    assert enviados == [("5584999990000", "Atrasado")]


def test_sem_banco_nao_envia_atrasado_o_que_e_de_antes_da_subida(cenario):
    leitor, fila, agenda, store, enviados, relogio = cenario
    agenda.poe("e1", "Enviar no WhatsApp: 84999990000", "Velho", "2026-11-14T08:30:00-03:00")
    r = leitor.sync()
    assert r["agendados"] == [] and r["ignorados"]
    fila.check()
    assert enviados == []


def test_enviado_e_remarcado_para_o_futuro_sai_de_novo(cenario):
    leitor, fila, agenda, store, enviados, relogio = cenario
    agenda.poe("e1", "Enviar no WhatsApp: 84999990000", "Oi", "2026-11-14T09:30:00-03:00")
    leitor.sync()
    relogio.avanca(minutes=40)
    fila.check()
    assert len(enviados) == 1
    agenda.poe("e1", "Enviar no WhatsApp: 84999990000", "Oi", "2026-11-14T15:00:00-03:00")
    assert len(leitor.sync()["atualizados"]) == 1
    relogio.avanca(hours=6)
    fila.check()
    assert len(enviados) == 2


def test_janela_lida_volta_uma_leitura_e_vai_ate_o_horizonte(cenario):
    leitor, fila, agenda, store, enviados, relogio = cenario
    leitor.sync()
    inicio, fim = agenda.janelas[-1]
    assert inicio == AGORA - timedelta(seconds=3600 + 600)
    assert fim == AGORA + timedelta(hours=24)
