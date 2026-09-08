"""
Testes das regras de validacao.

Mesma filosofia do test_limpeza: a configuracao usada nos testes vem de
tests/apoio.py, pequena e completa, para o teste falhar por causa da regra e nao
por causa de uma meta trocada no config.yaml. Os dois ultimos testes sao a
excecao - leem o config de verdade e conferem que ele continua compativel com o
que a validacao espera.

Cada regra tem um teste que a viola e um teste que passa raspando pelo limite. O
par importa: regra que so e testada com valor absurdo passa despercebida quando o
sinal da comparacao esta invertido.
"""

import sys
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from apoio import COLUNAS, cfg_teste, linhas
from src.config import carregar
from src.leitura import COLUNA_ARQUIVO, COLUNA_LINHA, Descarte
from src.limpeza import (
    STATUS_CANCELADA,
    STATUS_EM_PRODUCAO,
    STATUS_ENTREGUE,
    TIPO_NUMERICO_POR_COLUNA,
)
from src.validacao import (
    ACAO_CORRIGIDO,
    ACAO_DESCARTADO,
    ACAO_REJEITADO,
    COLUNA_MOTIVOS,
    MOTIVO_ENTREGA_ANTES_DA_ENTRADA,
    MOTIVO_ILEGIVEL,
    MOTIVO_OBRIGATORIO_VAZIO,
    MOTIVO_PRAZO_ANTES_DA_ENTRADA,
    MOTIVO_REFUGO_ACIMA_DO_LIMITE,
    MOTIVO_STATUS_INCOERENTE,
    MOTIVO_TEMPO_ACIMA_DO_MAXIMO,
    MOTIVO_TEMPO_TOTAL_ACIMA_DO_MAXIMO,
    MOTIVO_TIRAGEM_BOA_VAZIA,
    MOTIVO_VALOR_NEGATIVO,
    ErroValidacao,
    limpadores,
    ocorrencias_de_descartes,
    validar,
)

def motivos_da_primeira(resultado) -> list[str]:
    """Os motivos de rejeicao da primeira linha, ja separados."""
    if resultado.rejeitadas.empty:
        return []
    return resultado.rejeitadas.iloc[0][COLUNA_MOTIVOS].split("; ")


# ---------------------------------------------------------------------------
# Caminho feliz
# ---------------------------------------------------------------------------

def test_linha_coerente_e_aprovada():
    resultado = validar(linhas({}), cfg_teste())
    assert len(resultado.aprovadas) == 1
    assert resultado.rejeitadas.empty
    assert resultado.ocorrencias == []
    assert resultado.taxa_rejeicao == 0.0


def test_aprovada_sai_com_os_valores_limpos():
    resultado = validar(
        linhas({"maquina": "  HEIDELBERG  SM-52 ", "tiragem_boa": "5.100"}),
        cfg_teste(),
    )
    aprovada = resultado.aprovadas.iloc[0]
    assert aprovada["maquina"] == "Heidelberg SM52"
    assert aprovada["tiragem_boa"] == 5100
    assert aprovada["data_entrada"] == date(2026, 3, 2)


def test_ordem_cancelada_sem_tiragem_boa_e_aprovada():
    # Cancelada depois do acerto: o setup foi gasto, nada foi produzido.
    resultado = validar(
        linhas({
            "status": STATUS_CANCELADA,
            "data_entrega": None,
            "tiragem_boa": None,
            "refugo": 0,
            "tempo_producao_min": 0.0,
        }),
        cfg_teste(),
    )
    assert len(resultado.aprovadas) == 1


def test_rastreio_de_origem_sobrevive_a_validacao():
    resultado = validar(linhas({}), cfg_teste())
    aprovada = resultado.aprovadas.iloc[0]
    assert aprovada[COLUNA_ARQUIVO] == "planilha.xlsx"
    assert aprovada[COLUNA_LINHA] == 2


# ---------------------------------------------------------------------------
# Correcao vira ocorrencia, nao rejeicao
# ---------------------------------------------------------------------------

def test_valor_corrigido_gera_ocorrencia_e_aprova():
    resultado = validar(linhas({"maquina": "HEIDELBERG  SM-52 "}), cfg_teste())

    assert len(resultado.aprovadas) == 1
    assert resultado.linhas_corrigidas == 1

    ocorrencia = resultado.ocorrencias[0]
    assert ocorrencia.acao == ACAO_CORRIGIDO
    assert ocorrencia.coluna == "maquina"
    assert ocorrencia.valor_original == "HEIDELBERG  SM-52 "
    assert ocorrencia.valor_final == "Heidelberg SM52"
    assert ocorrencia.linha_planilha == 2


def test_serial_do_excel_e_corrigido_e_nao_rejeitado():
    # 46086 = 05/03/2026 no sistema de datas de 1900.
    resultado = validar(linhas({"data_prazo": 46086}), cfg_teste())
    assert len(resultado.aprovadas) == 1
    assert resultado.aprovadas.iloc[0]["data_prazo"] == date(2026, 3, 5)


# ---------------------------------------------------------------------------
# Uma regra por vez
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("coluna", ["numero_os", "maquina", "data_entrada"])
def test_campo_obrigatorio_vazio_rejeita(coluna):
    resultado = validar(linhas({coluna: None}), cfg_teste())
    assert MOTIVO_OBRIGATORIO_VAZIO in motivos_da_primeira(resultado)


def test_campo_opcional_vazio_nao_rejeita():
    resultado = validar(linhas({"cliente": None, "operador": None}), cfg_teste())
    assert len(resultado.aprovadas) == 1


def test_tiragem_boa_vazia_fora_de_cancelada_rejeita():
    resultado = validar(linhas({"tiragem_boa": None}), cfg_teste())
    assert MOTIVO_TIRAGEM_BOA_VAZIA in motivos_da_primeira(resultado)


def test_tiragem_boa_vazia_sem_status_rejeita():
    # Sem status nao ha como afirmar que a ordem foi cancelada.
    resultado = validar(linhas({"tiragem_boa": None, "status": None}), cfg_teste())
    assert MOTIVO_TIRAGEM_BOA_VAZIA in motivos_da_primeira(resultado)


@pytest.mark.parametrize("coluna", sorted(TIPO_NUMERICO_POR_COLUNA))
def test_valor_negativo_rejeita_em_qualquer_coluna_numerica(coluna):
    resultado = validar(linhas({coluna: -1}), cfg_teste())
    assert MOTIVO_VALOR_NEGATIVO in motivos_da_primeira(resultado)


def test_zero_nao_e_negativo():
    resultado = validar(
        linhas({
            "status": STATUS_CANCELADA,
            "data_entrega": None,
            "tiragem_boa": 0,
            "refugo": 0,
            "tempo_producao_min": 0.0,
        }),
        cfg_teste(),
    )
    assert len(resultado.aprovadas) == 1


def test_tempo_acima_do_maximo_rejeita():
    resultado = validar(
        linhas({"tempo_setup_min": 0.0, "tempo_producao_min": 2880.5}), cfg_teste()
    )
    assert MOTIVO_TEMPO_ACIMA_DO_MAXIMO in motivos_da_primeira(resultado)


def test_tempo_exatamente_no_maximo_passa():
    resultado = validar(
        linhas({"tempo_setup_min": 0.0, "tempo_producao_min": 2880.0}), cfg_teste()
    )
    assert len(resultado.aprovadas) == 1


def test_soma_dos_tempos_acima_do_maximo_rejeita():
    # 40h de producao com 20h de acerto: cada metade cabe no teto por coluna, a
    # ordem somada ocupa 60h de maquina.
    resultado = validar(
        linhas({"tempo_setup_min": 1200.0, "tempo_producao_min": 2400.0}), cfg_teste()
    )
    motivos = motivos_da_primeira(resultado)
    assert MOTIVO_TEMPO_TOTAL_ACIMA_DO_MAXIMO in motivos
    assert MOTIVO_TEMPO_ACIMA_DO_MAXIMO not in motivos


def test_soma_exatamente_no_maximo_passa():
    resultado = validar(
        linhas({"tempo_setup_min": 880.0, "tempo_producao_min": 2000.0}), cfg_teste()
    )
    assert len(resultado.aprovadas) == 1


def test_tempo_negativo_nao_dispara_a_regra_da_soma():
    # Somar um tempo impossivel nao significa nada; a linha ja foi rejeitada
    # pela celula certa.
    resultado = validar(
        linhas({"tempo_setup_min": -3000.0, "tempo_producao_min": 34.0}), cfg_teste()
    )
    assert motivos_da_primeira(resultado) == [MOTIVO_VALOR_NEGATIVO]


def test_uma_coluna_absurda_com_a_outra_zerada_e_pega_pelo_teto_por_coluna():
    resultado = validar(
        linhas({"tempo_setup_min": 4000.0, "tempo_producao_min": 0.0}), cfg_teste()
    )
    motivos = motivos_da_primeira(resultado)
    assert MOTIVO_TEMPO_ACIMA_DO_MAXIMO in motivos
    assert MOTIVO_TEMPO_TOTAL_ACIMA_DO_MAXIMO in motivos


def test_refugo_acima_do_limite_rejeita():
    # Limite = 50% de 5000 = 2500.
    resultado = validar(linhas({"refugo": 2501}), cfg_teste())
    assert MOTIVO_REFUGO_ACIMA_DO_LIMITE in motivos_da_primeira(resultado)


def test_refugo_exatamente_no_limite_passa():
    resultado = validar(linhas({"refugo": 2500}), cfg_teste())
    assert len(resultado.aprovadas) == 1


def test_refugo_nao_e_avaliado_sem_tiragem_prevista():
    # Sem o pedido do cliente nao ha base de comparacao. A ausencia em si nao e
    # violacao: 'tiragem_prevista' nao esta em 'validacao.obrigatorios'.
    resultado = validar(linhas({"tiragem_prevista": None, "refugo": 9999}), cfg_teste())
    assert len(resultado.aprovadas) == 1


def test_entrega_anterior_a_entrada_rejeita():
    resultado = validar(linhas({"data_entrega": date(2026, 2, 25)}), cfg_teste())
    assert MOTIVO_ENTREGA_ANTES_DA_ENTRADA in motivos_da_primeira(resultado)


def test_entrega_no_mesmo_dia_da_entrada_passa():
    resultado = validar(linhas({"data_entrega": date(2026, 3, 2)}), cfg_teste())
    assert len(resultado.aprovadas) == 1


def test_prazo_anterior_a_entrada_rejeita():
    resultado = validar(linhas({"data_prazo": date(2026, 2, 25)}), cfg_teste())
    assert MOTIVO_PRAZO_ANTES_DA_ENTRADA in motivos_da_primeira(resultado)


def test_entregue_sem_data_de_entrega_rejeita():
    resultado = validar(linhas({"data_entrega": None}), cfg_teste())
    assert MOTIVO_STATUS_INCOERENTE in motivos_da_primeira(resultado)


@pytest.mark.parametrize("status", [STATUS_EM_PRODUCAO, STATUS_CANCELADA])
def test_nao_entregue_com_data_de_entrega_rejeita(status):
    resultado = validar(linhas({"status": status}), cfg_teste())
    assert MOTIVO_STATUS_INCOERENTE in motivos_da_primeira(resultado)


def test_em_producao_sem_data_de_entrega_passa():
    # Ordem legitimamente aberta: e o caso normal, nao sujeira.
    resultado = validar(
        linhas({"status": STATUS_EM_PRODUCAO, "data_entrega": None}), cfg_teste()
    )
    assert len(resultado.aprovadas) == 1


# ---------------------------------------------------------------------------
# Valor que nao deu para limpar
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "alteracao",
    [
        {"maquina": "Roland 700"},
        {"turno": "4"},
        {"status": "PARADA"},
        {"data_entrada": "31/02/2026"},
        {"tiragem_boa": "cinco mil"},
        {"tempo_setup_min": "trinta"},
    ],
)
def test_valor_ilegivel_rejeita_a_linha(alteracao):
    resultado = validar(linhas(alteracao), cfg_teste())
    assert MOTIVO_ILEGIVEL in motivos_da_primeira(resultado)


def test_ocorrencia_de_ilegivel_carrega_o_valor_que_causou():
    resultado = validar(linhas({"maquina": "Roland 700"}), cfg_teste())
    ocorrencia = next(o for o in resultado.ocorrencias if o.acao == ACAO_REJEITADO)
    assert ocorrencia.coluna == "maquina"
    assert ocorrencia.valor_original == "Roland 700"
    assert "Roland 700" in ocorrencia.problema
    assert ocorrencia.valor_final == ""


# ---------------------------------------------------------------------------
# Varias regras na mesma linha
# ---------------------------------------------------------------------------

def test_linha_com_dois_problemas_reporta_os_dois():
    resultado = validar(
        linhas({"refugo": 4000, "data_entrega": date(2026, 1, 1)}), cfg_teste()
    )
    motivos = motivos_da_primeira(resultado)
    assert MOTIVO_REFUGO_ACIMA_DO_LIMITE in motivos
    assert MOTIVO_ENTREGA_ANTES_DA_ENTRADA in motivos
    assert resultado.linhas_rejeitadas == 1


def test_motivo_repetido_na_mesma_linha_aparece_uma_vez_so():
    resultado = validar(
        linhas({"tempo_setup_min": -1.0, "tempo_producao_min": -2.0}), cfg_teste()
    )
    assert motivos_da_primeira(resultado) == [MOTIVO_VALOR_NEGATIVO]
    # Mas as duas celulas continuam nomeadas no relatorio.
    rejeitadas = [o for o in resultado.ocorrencias if o.acao == ACAO_REJEITADO]
    assert {o.coluna for o in rejeitadas} == {"tempo_setup_min", "tempo_producao_min"}


def test_correcao_de_linha_rejeitada_continua_no_relatorio():
    # A celula foi corrigida de fato; quem le o CSV ve as duas linhas com o
    # mesmo numero de linha da planilha e entende o desfecho.
    resultado = validar(linhas({"maquina": " sm 52 ", "refugo": 4000}), cfg_teste())
    acoes = {o.acao for o in resultado.ocorrencias}
    assert acoes == {ACAO_CORRIGIDO, ACAO_REJEITADO}


# ---------------------------------------------------------------------------
# Taxa de rejeicao
# ---------------------------------------------------------------------------

def test_taxa_de_rejeicao_sobre_o_total_lido():
    resultado = validar(linhas({}, {}, {}, {"numero_os": None}), cfg_teste())
    assert resultado.total_linhas == 4
    assert resultado.linhas_rejeitadas == 1
    assert resultado.taxa_rejeicao == 0.25


def test_taxa_de_rejeicao_de_dataframe_vazio_e_zero():
    vazio = pd.DataFrame(
        columns=list(COLUNAS) + [COLUNA_ARQUIVO, COLUNA_LINHA], dtype=object
    )
    resultado = validar(vazio, cfg_teste())
    assert resultado.total_linhas == 0
    assert resultado.taxa_rejeicao == 0.0
    assert not resultado.acima_do_limite(cfg_teste())


def test_acima_do_limite_compara_com_o_config():
    cfg = cfg_teste()
    # 1 de 4 = 25%, acima do limite de 10%.
    resultado = validar(linhas({}, {}, {}, {"numero_os": None}), cfg)
    assert resultado.acima_do_limite(cfg)


def test_no_limite_exato_nao_e_acima():
    validacao = dict(cfg_teste().validacao)
    validacao["taxa_rejeicao_limite"] = 0.25
    cfg = cfg_teste(validacao=validacao)
    resultado = validar(linhas({}, {}, {}, {"numero_os": None}), cfg)
    assert resultado.taxa_rejeicao == 0.25
    assert not resultado.acima_do_limite(cfg)


def test_contagem_por_motivo_conta_linhas():
    resultado = validar(
        linhas({"numero_os": None}, {"maquina": None}, {"refugo": 4000}), cfg_teste()
    )
    assert resultado.linhas_por_motivo[MOTIVO_OBRIGATORIO_VAZIO] == 2
    assert resultado.linhas_por_motivo[MOTIVO_REFUGO_ACIMA_DO_LIMITE] == 1


# ---------------------------------------------------------------------------
# Descartes da leitura
# ---------------------------------------------------------------------------

def test_descarte_da_leitura_vira_ocorrencia_descartada():
    ocorrencias = ocorrencias_de_descartes(
        [Descarte("planilha.xlsx", 251, "linha de rodape (somatorio)", "numero_os='TOTAL'")]
    )
    assert len(ocorrencias) == 1
    assert ocorrencias[0].acao == ACAO_DESCARTADO
    assert ocorrencias[0].linha_planilha == 251
    assert ocorrencias[0].problema == "linha de rodape (somatorio)"


def test_descarte_nao_entra_na_taxa_de_rejeicao():
    resultado = validar(linhas({}), cfg_teste())
    resultado.ocorrencias.extend(
        ocorrencias_de_descartes([Descarte("planilha.xlsx", 251, "rodape", "")])
    )
    assert resultado.taxa_rejeicao == 0.0
    assert resultado.total_linhas == 1


# ---------------------------------------------------------------------------
# Guardas de configuracao
# ---------------------------------------------------------------------------

def test_coluna_sem_limpador_recusa():
    colunas = {coluna: [coluna.upper()] for coluna in COLUNAS}
    colunas["custo_chapa"] = ["CUSTO CHAPA"]
    with pytest.raises(ErroValidacao, match="custo_chapa"):
        limpadores(cfg_teste(colunas=colunas))


def test_limpador_sem_coluna_recusa():
    colunas = {coluna: [coluna.upper()] for coluna in COLUNAS if coluna != "refugo"}
    with pytest.raises(ErroValidacao, match="refugo"):
        limpadores(cfg_teste(colunas=colunas))


def test_status_renomeado_no_config_recusa():
    status = {"FINALIZADA": ["entregue"], STATUS_CANCELADA: ["canc"]}
    with pytest.raises(ErroValidacao, match=STATUS_ENTREGUE):
        validar(linhas({}), cfg_teste(status=status))


# ---------------------------------------------------------------------------
# Contra o config.yaml de verdade
# ---------------------------------------------------------------------------

def test_config_real_cobre_todas_as_colunas():
    # Se alguem acrescentar uma coluna no config.yaml sem limpador, e aqui que
    # aparece - antes de o valor cru chegar ao banco.
    assert set(limpadores(carregar())) == set(carregar().colunas)


def test_config_real_declara_os_status_que_as_regras_consultam():
    cfg = carregar()
    for status in (STATUS_ENTREGUE, STATUS_CANCELADA):
        assert status in cfg.status
