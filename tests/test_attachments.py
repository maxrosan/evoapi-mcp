"""Arquivo sozinho na conversa pessoal: espera, pergunta, e liga a resposta ao arquivo."""

import pytest

from evoapi_mcp.attachments import QUESTION_TTL_S, AttachmentAsker, question_text
from evoapi_mcp.webhook import EventLog
from test_webhook import evento

DONO = "5584999290327"
LID = "110818863673433@lid"
ALT = "5584999290327@s.whatsapp.net"
KEILLA = "558498140038@s.whatsapp.net"
MANUAL = "7.2 MANUAL DO DOCENTE DA FACULDADE DO SERIDÓ.pdf"


def _id(e, mid):
    e["data"]["key"]["id"] = mid
    if e["data"]["key"]["remoteJid"] == LID:
        e["data"]["key"]["remoteJidAlt"] = ALT
    return e


def documento(mid, nome=MANUAL, jid=LID, from_me=True):
    e = evento(None, from_me=from_me, jid=jid, tipo="documentMessage")
    e["data"]["message"] = {"documentMessage": {"fileName": nome, "mimetype": "application/pdf"}}
    return _id(e, mid)


def imagem(mid, legenda=None, jid=LID):
    e = evento(None, jid=jid, tipo="imageMessage")
    img = {"mimetype": "image/jpeg"}
    if legenda:
        img["caption"] = legenda
    e["data"]["message"] = {"imageMessage": img}
    return _id(e, mid)


def texto(mid, t, jid=LID, citando=None):
    e = evento(t, jid=jid)
    if citando:
        e["data"]["message"] = {"extendedTextMessage": {"text": t, "contextInfo": {
            "stanzaId": citando, "quotedMessage": {"conversation": "📎 Recebi ..."}}}}
    return _id(e, mid)


def audio(mid):
    e = evento(None, jid=LID, tipo="audioMessage")
    e["data"]["message"] = {"audioMessage": {"mimetype": "audio/ogg; codecs=opus", "ptt": True}}
    return _id(e, mid)


class Relogio:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


@pytest.fixture
def cena(monkeypatch):
    relogio = Relogio()
    monkeypatch.setattr("evoapi_mcp.webhook.time.time", relogio)
    log = EventLog(owner_number=DONO)
    log.hook_min_age_s = 0
    enviados = []

    def send(numero, t):
        enviados.append((numero, t))
        return {"key": {"id": f"Q{len(enviados)}"}}

    asker = AttachmentAsker(log, send, DONO, clock=relogio)
    log.attachments = asker
    return log, asker, enviados, relogio


# ---------------------------------------------------------------- pergunta

def test_texto_da_pergunta():
    um = question_text([{"id": "D1", "tipo": "document", "arquivo": MANUAL}])
    assert MANUAL in um
    assert "O que faço com isso?" in um
    assert "uma imagem" in question_text([{"id": "I1", "tipo": "image"}])
    varios = question_text([{"id": f"D{i}", "tipo": "document", "arquivo": f"a{i}.pdf"} for i in range(5)])
    assert "Recebi 5 arquivos" in varios
    assert "e mais 2" in varios


def test_pdf_sozinho_gera_uma_pergunta(cena):
    log, asker, enviados, relogio = cena
    log.add(documento("D1"))
    relogio.t += 30
    assert asker.check() is None
    relogio.t += 31
    pergunta = asker.check()
    assert pergunta["arquivos"] == [{"id": "D1", "tipo": "document", "arquivo": MANUAL, "mime": "application/pdf"}]
    assert enviados == [(DONO, pergunta["texto"])]
    assert log.is_sent_by_assistant("Q1")
    assert log.store.is_handled("Q1")
    assert asker.check() is None
    assert asker.describe()["perguntas_abertas"] == 1
    assert asker.describe()["arquivos_aguardando"] == 0


def test_texto_depois_do_pdf_nao_pergunta(cena):
    log, asker, enviados, relogio = cena
    log.add(documento("D1"))
    relogio.t += 20
    log.add(texto("T1", "arquiva isso"))
    relogio.t += 61
    assert asker.check() is None
    assert enviados == []


def test_audio_depois_do_pdf_nao_pergunta(cena):
    log, asker, enviados, relogio = cena
    log.add(documento("D1"))
    relogio.t += 10
    assert log.add(audio("A1"))["voice_pending"] is True
    relogio.t += 61
    assert asker.check() is None
    assert enviados == []


def test_arquivo_entregue_como_contexto_nao_pergunta(cena):
    log, asker, enviados, relogio = cena
    log.add(documento("D1"))
    assert asker.consume(["D1"]) == 1
    relogio.t += 61
    assert asker.check() is None
    assert enviados == []


def test_varios_arquivos_viram_uma_pergunta_e_esperam_o_ultimo(cena):
    log, asker, enviados, relogio = cena
    log.add(documento("D1"))
    relogio.t += 50
    log.add(imagem("I1"))
    relogio.t += 20
    assert asker.check() is None
    relogio.t += 41
    pergunta = asker.check()
    assert [a["id"] for a in pergunta["arquivos"]] == ["D1", "I1"]
    assert len(enviados) == 1
    assert "Recebi 2 arquivos" in enviados[0][1]


def test_arquivo_em_outra_conversa_ou_de_terceiro_nao_pergunta(cena):
    log, asker, enviados, relogio = cena
    log.add(documento("D1", jid=KEILLA))
    log.add(documento("D2", from_me=False))
    relogio.t += 61
    assert asker.check() is None
    assert asker.describe()["arquivos_aguardando"] == 0


def test_imagem_com_legenda_e_instrucao_e_nao_aguarda(cena):
    log, asker, enviados, relogio = cena
    assert log.add(imagem("I1", legenda="coloca no Trello"))["trigger"] is True
    assert asker.describe()["arquivos_aguardando"] == 0


def test_falha_ao_perguntar_nao_derruba(cena):
    log, asker, enviados, relogio = cena

    def explode(numero, t):
        raise RuntimeError("Evolution fora")

    asker.send = explode
    log.add(documento("D1"))
    relogio.t += 61
    assert asker.check() is None
    assert asker.describe()["arquivos_aguardando"] == 0


# ---------------------------------------------------------------- resposta

def test_resposta_seguinte_leva_a_pergunta_e_os_arquivos(cena):
    log, asker, enviados, relogio = cena
    log.add(documento("D1"))
    relogio.t += 61
    asker.check()
    relogio.t += 30
    log.add(texto("T2", "indexa e arquiva na pasta da FAS"))
    pendente = log.pending()[0]
    assert pendente["question"]["id"] == "Q1"
    assert pendente["files_in_question"][0]["id"] == "D1"
    assert asker.describe()["perguntas_abertas"] == 0


def test_resposta_citando_a_pergunta_vale_depois_do_prazo(cena):
    log, asker, enviados, relogio = cena
    log.add(documento("D1"))
    relogio.t += 61
    asker.check()
    relogio.t += QUESTION_TTL_S + 10
    log.add(texto("T2", "resume para mim", citando="Q1"))
    pendente = log.pending()[0]
    assert pendente["files_in_question"][0]["id"] == "D1"
    assert pendente.get("reply_to") is None


def test_resposta_depois_do_prazo_sem_citar_nao_liga(cena):
    log, asker, enviados, relogio = cena
    log.add(documento("D1"))
    relogio.t += 61
    asker.check()
    relogio.t += QUESTION_TTL_S + 10
    log.add(texto("T2", "outra coisa"))
    assert "question" not in log.pending()[0]


def test_eco_do_assistente_nao_suprime_a_pergunta(cena):
    log, asker, enviados, relogio = cena
    log.add(documento("D1"))
    log.note_sent("ECO1")
    relogio.t += 10
    log.add(texto("ECO1", "resposta do assistente a outro pedido"))
    relogio.t += 61
    assert asker.check() is not None


def test_ganchos_esperam_a_idade_minima(cena):
    log, asker, enviados, relogio = cena

    class Historico:
        def __init__(self):
            self.abertos = []

        def open(self, e):
            self.abertos.append(e["message_id"])

    log.history = Historico()
    log.hook_min_age_s = 3
    log.add(texto("T1", "faça X"))
    log.pending()
    assert log.history.abertos == []
    relogio.t += 4
    log.pending()
    log.pending()
    assert log.history.abertos == ["T1"]


def test_eco_do_assistente_nao_abre_historico(cena):
    log, asker, enviados, relogio = cena

    class Historico:
        abertos = []

        def open(self, e):
            self.abertos.append(e["message_id"])

    log.history = Historico()
    log.note_sent("ECO1")
    log.add(texto("ECO1", "mensagem do assistente"))
    log.pending()
    assert Historico.abertos == []
