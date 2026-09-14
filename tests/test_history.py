"""Histórico de conversas: pedido, resposta, ligação por conversa, vetor ao fechar e busca."""

import pytest

from evoapi_mcp.history import OPEN_TTL_S, ConversationLog, HistoryError
from evoapi_mcp.jobs import JobQueues
from evoapi_mcp.store import MemoryStore
from evoapi_mcp.webhook import EventLog
from test_memory import FakeEmbedder

DONO = "5584999290327"
KEILLA = "558498140038@s.whatsapp.net"
PESSOAL_LID = "110818863673433@lid"


class Relogio:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


def pedido(mid, texto, chat=KEILLA, self_chat=False, alt=None, voice=False, reply_to=None):
    return {
        "message_id": mid, "instruction": texto, "chat_jid": chat, "chat": chat.split("@")[0],
        "chat_alt": alt, "self_chat": True if self_chat else None, "voice": True if voice else None,
        "reply_to": reply_to, "trigger": True,
    }


@pytest.fixture
def relogio():
    return Relogio()


@pytest.fixture
def historico(relogio):
    return ConversationLog(MemoryStore(), FakeEmbedder(), JobQueues(sync=True), owner_number=DONO, clock=relogio)


def test_pedido_resposta_e_busca(historico):
    cid = historico.open(pedido("P1", "envie para Keilla um vídeo sobre produção de café"))
    assert historico.response(KEILLA, None, "Keilla, segue um vídeo sobre produção de café") == cid
    assert historico.close("P1") == cid
    linha = historico.store.get_conversation_by_trigger("P1")
    assert linha["embedding"] is not None
    assert linha["handled_at"] is not None
    achado = historico.search("vídeo de café para a Keilla")[0]
    assert achado["id"] == cid
    assert "café" in achado["pedido"]
    assert "segue um vídeo" in achado["resposta"]


def test_resposta_pelo_numero_liga_pedido_da_conversa_pessoal(historico):
    cid = historico.open(pedido("P1", "resuma as notas", chat=PESSOAL_LID,
                                alt=f"{DONO}@s.whatsapp.net", self_chat=True))
    assert historico.response("558499290327@s.whatsapp.net", None, "resumo: ...") == cid


def test_resposta_de_outra_conversa_nao_liga(historico):
    historico.open(pedido("P1", "faça X"))
    assert historico.response("5511999999999@s.whatsapp.net", None, "outra coisa") is None


def test_resposta_sem_pedido_aberto_ou_vazia_e_ignorada(historico):
    assert historico.response(KEILLA, None, "mensagem avulsa") is None
    historico.open(pedido("P1", "faça X"))
    assert historico.response(KEILLA, None, "   ") is None


def test_pedido_velho_nao_recebe_resposta(historico, relogio):
    historico.open(pedido("P1", "faça X"))
    relogio.t += OPEN_TTL_S + 1
    assert historico.response(KEILLA, None, "atrasada") is None


def test_respostas_se_juntam_no_pedido_mais_recente(historico, relogio):
    historico.open(pedido("P1", "primeiro pedido"))
    relogio.t += 10
    c2 = historico.open(pedido("P2", "segundo pedido"))
    historico.response(KEILLA, None, "parte 1")
    historico.response(KEILLA, None, "parte 2")
    segunda = historico.store.get_conversation_by_trigger("P2")
    assert segunda["id"] == c2
    assert segunda["response"] == "parte 1\nparte 2"
    assert historico.store.get_conversation_by_trigger("P1")["response"] is None


def test_mesmo_pedido_duas_vezes_nao_duplica(historico):
    assert historico.open(pedido("P1", "faça X")) == historico.open(pedido("P1", "faça X"))
    assert historico.store.count_conversations() == 1


def test_evento_sem_instrucao_nao_abre(historico):
    assert historico.open({"message_id": "P1", "instruction": None}) is None


def test_voz_e_citacao_sao_gravadas(historico):
    historico.open(pedido("P1", "arquiva isso", voice=True, reply_to={"id": "D1", "file": "boleto.pdf"}))
    linha = historico.store.get_conversation_by_trigger("P1")
    assert linha["voice"] is True
    assert linha["quoted"] == "boleto.pdf"


def test_fechar_depois_de_reiniciar_recupera_do_banco(relogio):
    store = MemoryStore()
    antes = ConversationLog(store, FakeEmbedder(), JobQueues(sync=True), owner_number=DONO, clock=relogio)
    antes.open(pedido("P1", "quadro do NARA no Trello"))
    antes.response(KEILLA, None, "fica no Kanban - Nara")
    depois = ConversationLog(store, FakeEmbedder(), JobQueues(sync=True), owner_number=DONO, clock=relogio)
    assert depois.close("P1") is not None
    assert "Kanban" in depois.search("quadro do NARA")[0]["resposta"]


def test_fechar_desconhecido_ou_ja_fechado(historico):
    assert historico.close("NAO_EXISTE") is None
    historico.open(pedido("P1", "faça X"))
    historico.close("P1")
    assert historico.close("P1") is None


def test_busca_depois_de_carregada_ve_conversas_novas(historico):
    historico.open(pedido("P1", "boleto da Aldann"))
    historico.close("P1")
    assert historico.search("boleto Aldann")
    historico.open(pedido("P2", "nota da Econtec"))
    historico.close("P2")
    assert historico.search("nota Econtec")[0]["pedido"] == "nota da Econtec"


def test_busca_filtra_por_chat_e_valida(historico):
    historico.open(pedido("P1", "boleto da Aldann"))
    historico.close("P1")
    historico.open(pedido("P2", "boleto da Paiva", chat="5511999999999@s.whatsapp.net"))
    historico.close("P2")
    assert [c["chat"] for c in historico.search("boleto", chat="5511999999999")] == ["5511999999999@s.whatsapp.net"]
    with pytest.raises(HistoryError):
        historico.search("  ")


def test_busca_sem_conversas(historico):
    assert historico.search("qualquer coisa") == []


def test_describe(historico):
    historico.open(pedido("P1", "faça X"))
    assert historico.describe() == {"ativo": True, "conversas": 1, "abertas": 1}


def test_eventlog_abre_e_fecha_o_historico(historico):
    from test_webhook import evento

    log = EventLog(store=historico.store, owner_number=DONO)
    log.history = historico
    e = evento("IA: coloque o bug no Trello", jid=KEILLA)
    e["data"]["key"]["id"] = "T1"
    log.add(e)
    assert historico.store.get_conversation_by_trigger("T1")["request"] == "coloque o bug no Trello"
    historico.response(KEILLA, None, "card criado")
    log.mark_handled(["T1"])
    assert historico.store.get_conversation_by_trigger("T1")["embedding"] is not None


def test_falha_no_historico_nao_derruba_o_acionamento():
    from test_webhook import evento

    class Quebrado:
        def open(self, e):
            raise RuntimeError("banco fora")

        def close(self, mid):
            raise RuntimeError("banco fora")

    log = EventLog(owner_number=DONO)
    log.history = Quebrado()
    e = evento("IA: faça X", jid=KEILLA)
    e["data"]["key"]["id"] = "T1"
    assert log.add(e)["trigger"] is True
    assert log.mark_handled(["T1"]) == 1
