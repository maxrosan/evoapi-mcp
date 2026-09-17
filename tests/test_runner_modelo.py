"""Executor local: pedido de investigação vai para o modelo maior, o resto não."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "runner"))
watch = pytest.importorskip("watch")


def _p(instrucao, respondendo_a=None):
    return {"id": "X", "chat": "c", "instrucao": instrucao, "respondendo_a": respondendo_a}


@pytest.mark.parametrize("instrucao", [
    "por que Nara conta como zero planejamentos finalizados para UNiCA?",
    "Por quê a tela não carrega?",
    "investigue a conta da Silvana",
    "tem um bug no relatório",
    "deu erro ao salvar",
    "o login está falhando",
    "quebrou o cadastro",
    "a exportação não funciona",
    "a exportação não está funcionando",
    "faça um diagnóstico disso",
    "opus, analisa esse contrato",
])
def test_investigacao_usa_o_modelo_maior(instrucao):
    modelo, limite, motivo = watch.escolher_modelo([_p(instrucao)])
    assert modelo == watch.INVESTIGATION_MODEL
    assert limite >= watch.CLAUDE_TIMEOUT_S
    assert motivo


@pytest.mark.parametrize("instrucao", [
    "arquiva esse boleto na pasta da MR",
    "manda um áudio para a Keilla dizendo que chego às 9h",
    "coloque no Trello da FAS",
    "resuma as notas de agosto",
    "ele não veio porque choveu",
    "Ferrovia e ferro velho",
    "errado não, pode mandar",
])
def test_pedido_comum_fica_no_modelo_padrao(instrucao):
    assert watch.escolher_modelo([_p(instrucao)]) == (watch.CLAUDE_MODEL, watch.CLAUDE_TIMEOUT_S, None)


def test_uma_investigacao_no_lote_basta_e_resposta_citada_conta():
    modelo, _, _ = watch.escolher_modelo([_p("arquiva isso"), _p("por que caiu?")])
    assert modelo == watch.INVESTIGATION_MODEL
    modelo, _, motivo = watch.escolher_modelo([_p("sim", respondendo_a="Quer que eu investigue o bug?")])
    assert modelo == watch.INVESTIGATION_MODEL
    assert watch.escolher_modelo([]) == (watch.CLAUDE_MODEL, watch.CLAUDE_TIMEOUT_S, None)
