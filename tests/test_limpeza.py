"""
Testes das funcoes de limpeza.

Cobrem os formatos da secao 4.2 da especificacao, um por um. Os sinonimos vem
de dicionarios pequenos escritos aqui, para o teste falhar por causa da funcao e
nao por causa de uma mudanca no config.yaml. O ultimo teste e a excecao: ele le
o config de verdade e confere que toda variante declarada la casa.
"""

import sys
from datetime import date, datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import carregar
from src.limpeza import (
    BASE_SERIAL_EXCEL,
    FAIXA_ANOS_SERIAL,
    TIPO_NUMERICO_POR_COLUNA,
    chave_comparacao,
    converter_data,
    converter_numero,
    normalizar_maquina,
    normalizar_status,
    normalizar_texto,
    normalizar_turno,
    sem_acento,
)

MAQUINAS = {
    "Heidelberg SM52": ["heidelberg sm52", "heidelberg sm-52", "heid sm52", "sm 52"],
    "Polar 115 (Guilhotina)": ["polar 115", "polar", "guilhotina"],
}
TURNOS = {
    "1": ["1", "1o", "primeiro", "manha", "a"],
    "2": ["2", "2o", "segundo", "tarde", "b"],
}
STATUS = {
    "EM PRODUCAO": ["em producao", "em produção", "aberta", "wip"],
    "ENTREGUE": ["entregue", "concluida", "ok"],
}


# ---------------------------------------------------------------------------
# normalizar_texto
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "entrada, esperado",
    [
        ("OP-05", "OP-05"),
        ("  OP-05  ", "OP-05"),
        ("OP-05 ", "OP-05"),
        ("Folder  A4  4x4", "Folder A4 4x4"),
        ("  Catalogo   16pg  ", "Catalogo 16pg"),
    ],
)
def test_normalizar_texto_tira_espaco(entrada, esperado):
    resultado = normalizar_texto(entrada)
    assert resultado.valor == esperado
    assert resultado.problema is None


def test_normalizar_texto_sem_mudanca_nao_reporta_correcao():
    assert normalizar_texto("OP-05").correcao is None


def test_normalizar_texto_com_mudanca_reporta_correcao():
    assert normalizar_texto(" OP-05 ").correcao == "espaco sobrando removido"


def test_normalizar_texto_preserva_acento_e_caixa():
    # O texto vai para o painel como foi escrito. Quem compara usa chave_comparacao.
    assert normalizar_texto("Catálogo Institucional").valor == "Catálogo Institucional"


@pytest.mark.parametrize("vazio", [None, "", "   ", "\t\n"])
def test_normalizar_texto_vazio_nao_e_problema(vazio):
    resultado = normalizar_texto(vazio)
    assert resultado.valor is None
    assert resultado.problema is None
    assert resultado.correcao is None


def test_normalizar_texto_converte_nao_texto_e_avisa():
    resultado = normalizar_texto(1234)
    assert resultado.valor == "1234"
    assert "nao textual" in resultado.correcao


# ---------------------------------------------------------------------------
# chave_comparacao e sem_acento
# ---------------------------------------------------------------------------

def test_sem_acento_preserva_a_letra():
    assert sem_acento("em produção") == "em producao"
    assert sem_acento("concluída") == "concluida"


def test_chave_comparacao_junta_caixa_acento_e_espaco():
    assert chave_comparacao("HEIDELBERG  SM 52 ") == "heidelberg sm 52"
    assert chave_comparacao(" Em  PRODUÇÃO ") == "em producao"


# ---------------------------------------------------------------------------
# normalizar_maquina
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "entrada",
    [
        "Heidelberg SM52",
        "heidelberg sm52",
        "heidelberg sm-52",
        "HEIDELBERG  SM 52 ",
        "Heid SM52",
        "  heid  sm52  ",
        "SM 52",
    ],
)
def test_normalizar_maquina_reconhece_variantes(entrada):
    assert normalizar_maquina(entrada, MAQUINAS).valor == "Heidelberg SM52"


def test_normalizar_maquina_aceita_o_canonico_como_variante_de_si_mesmo():
    # 'Polar 115 (Guilhotina)' nao esta na lista de variantes. Sem esta regra, a
    # maioria das linhas - que vem escrita certa - seria rejeitada.
    resultado = normalizar_maquina("Polar 115 (Guilhotina)", MAQUINAS)
    assert resultado.valor == "Polar 115 (Guilhotina)"
    assert resultado.correcao is None
    assert resultado.problema is None


def test_normalizar_maquina_canonico_sujo_e_correcao_e_nao_problema():
    resultado = normalizar_maquina("  polar 115 (GUILHOTINA)  ", MAQUINAS)
    assert resultado.valor == "Polar 115 (Guilhotina)"
    assert resultado.correcao is not None
    assert resultado.problema is None


def test_normalizar_maquina_desconhecida_diz_o_valor_e_as_opcoes():
    resultado = normalizar_maquina("Roland 700", MAQUINAS)
    assert resultado.valor is None
    assert "'Roland 700'" in resultado.problema
    assert "Heidelberg SM52" in resultado.problema


def test_normalizar_maquina_vazia_nao_e_problema():
    assert normalizar_maquina(None, MAQUINAS).problema is None


def test_normalizar_maquina_ambigua_recusa_em_vez_de_escolher():
    # Sem espaco e sem hifen, 'sm 52' e 'sm-52' sao a mesma coisa. Escolher uma
    # das duas maquinas seria arbitrario e silencioso.
    ambiguo = {"Maquina A": ["sm 52"], "Maquina B": ["sm-52"]}
    resultado = normalizar_maquina("SM52", ambiguo)
    assert resultado.valor is None
    assert "ambiguo" in resultado.problema
    assert "Maquina A" in resultado.problema and "Maquina B" in resultado.problema


def test_normalizar_maquina_passo_compactado_so_entra_se_o_exato_falhar():
    # 'sm 52' casa exato com Maquina A, entao a ambiguidade nao chega a existir.
    ambiguo = {"Maquina A": ["sm 52"], "Maquina B": ["sm-52"]}
    assert normalizar_maquina("sm 52", ambiguo).valor == "Maquina A"
    assert normalizar_maquina("sm-52", ambiguo).valor == "Maquina B"


# ---------------------------------------------------------------------------
# normalizar_turno e normalizar_status
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "entrada, esperado",
    [("1", "1"), ("1o", "1"), ("primeiro", "1"), ("MANHA", "1"), (" a ", "1"),
     ("2", "2"), ("Tarde", "2"), ("B", "2")],
)
def test_normalizar_turno(entrada, esperado):
    assert normalizar_turno(entrada, TURNOS).valor == esperado


def test_normalizar_turno_numero_inteiro_da_planilha():
    # A celula pode chegar como int quando a coluna esta formatada como numero.
    assert normalizar_turno(1, TURNOS).valor == "1"


def test_normalizar_turno_invalido_reporta():
    resultado = normalizar_turno("4", TURNOS)
    assert resultado.valor is None
    assert "'4'" in resultado.problema


@pytest.mark.parametrize(
    "entrada, esperado",
    [("ENTREGUE", "ENTREGUE"), ("entregue", "ENTREGUE"), ("Ok", "ENTREGUE"),
     ("concluida", "ENTREGUE"), ("EM PRODUCAO", "EM PRODUCAO"),
     ("em produção", "EM PRODUCAO"), ("  WIP  ", "EM PRODUCAO")],
)
def test_normalizar_status_inclusive_com_acento(entrada, esperado):
    assert normalizar_status(entrada, STATUS).valor == esperado


# ---------------------------------------------------------------------------
# converter_data
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "entrada",
    ["05/03/2026", "2026-03-05", "5-mar-26", "05-03-2026", "2026/03/05",
     "5-marco-2026", "5-MAR-26"],
)
def test_converter_data_formatos_de_texto(entrada):
    assert converter_data(entrada).valor == date(2026, 3, 5)


def test_converter_data_serial_do_excel():
    # 46086 e 05/03/2026. Confere com as ancoras conhecidas do Excel.
    assert converter_data(46086).valor == date(2026, 3, 5)
    assert converter_data("46086").valor == date(2026, 3, 5)


@pytest.mark.parametrize(
    "serial, esperado",
    [(36526, date(2000, 1, 1)), (45292, date(2024, 1, 1)), (44197, date(2021, 1, 1))],
)
def test_converter_data_ancoras_conhecidas_do_excel(serial, esperado):
    assert converter_data(serial).valor == esperado


def test_base_do_serial_e_a_do_sistema_1900():
    assert (date(2024, 1, 1) - BASE_SERIAL_EXCEL).days == 45292


def test_converter_data_serial_fora_da_faixa_e_recusado():
    # 5000 seria 1913: numero digitado por engano numa coluna de data.
    resultado = converter_data(5000)
    assert resultado.valor is None
    assert "1913" in resultado.problema
    assert str(FAIXA_ANOS_SERIAL[0]) in resultado.problema


def test_converter_data_aceita_datetime_e_date():
    assert converter_data(datetime(2026, 3, 5)).valor == date(2026, 3, 5)
    assert converter_data(datetime(2026, 3, 5)).correcao is None
    assert converter_data(date(2026, 3, 5)).valor == date(2026, 3, 5)


def test_converter_data_descarta_hora_e_avisa():
    resultado = converter_data(datetime(2026, 3, 5, 14, 30))
    assert resultado.valor == date(2026, 3, 5)
    assert "14:30" in resultado.correcao


def test_converter_data_ambiguidade_ddmm_esta_assumida():
    # 05/03 e 5 de marco, nunca 3 de maio. Limitacao documentada no modulo.
    assert converter_data("05/03/2026").valor == date(2026, 3, 5)


def test_converter_data_ano_de_dois_digitos():
    assert converter_data("5-mar-26").valor.year == 2026
    assert converter_data("5-mar-98").valor.year == 1998


@pytest.mark.parametrize("entrada", ["nao e data", "32/13/2026", "5-xyz-26", "//"])
def test_converter_data_invalida_diz_o_valor(entrada):
    resultado = converter_data(entrada)
    assert resultado.valor is None
    assert repr(entrada) in resultado.problema or entrada in resultado.problema


@pytest.mark.parametrize("vazio", [None, "", "   "])
def test_converter_data_vazia_nao_e_problema(vazio):
    resultado = converter_data(vazio)
    assert resultado.valor is None
    assert resultado.problema is None


def test_converter_data_booleano_e_recusado():
    assert converter_data(True).problema is not None


# ---------------------------------------------------------------------------
# converter_numero
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "entrada, esperado",
    [
        ("5.000", 5000),
        ("5 000", 5000),
        ("5000,0", 5000),
        ("5000", 5000),
        ("1.234.567", 1234567),
        ("1 234 567", 1234567),
        ("12.500", 12500),
        (5000, 5000),
        (5000.0, 5000),
    ],
)
def test_converter_numero_formatos_da_especificacao(entrada, esperado):
    resultado = converter_numero(entrada, int)
    assert resultado.valor == esperado
    assert isinstance(resultado.valor, int)


@pytest.mark.parametrize(
    "entrada, esperado",
    [("41,6", 41.6), ("41.6", 41.6), ("0,5", 0.5), ("1.234,56", 1234.56),
     ("1,234.56", 1234.56), (41.6, 41.6)],
)
def test_converter_numero_com_casa_decimal(entrada, esperado):
    resultado = converter_numero(entrada, float)
    assert resultado.valor == pytest.approx(esperado)
    assert isinstance(resultado.valor, float)


def test_converter_numero_coluna_inteira_nao_tem_ambiguidade_de_separador():
    # Numa contagem, separador seguido de tres digitos e sempre milhar, sem
    # depender de quantos digitos vem antes. Era o furo da heuristica anterior.
    assert converter_numero("1234.567", int).valor == 1234567
    assert converter_numero("1234,567", int).valor == 1234567
    assert converter_numero("5.000", int).valor == 5000
    assert converter_numero("5,000", int).valor == 5000


def test_converter_numero_coluna_decimal_mantem_a_leitura_decimal():
    # A mesma cadeia numa coluna de tempo continua sendo decimal: la o valor
    # pode legitimamente ter casa decimal.
    assert converter_numero("1234.567", float).valor == pytest.approx(1234.567)


def test_converter_numero_coluna_inteira_recusa_casa_decimal():
    # '41.6' numa contagem nao vira 416 nem 42: vira ocorrencia no relatorio.
    resultado = converter_numero("41.6", int)
    assert resultado.valor is None
    assert "contagem inteira" in resultado.problema
    assert "41.6" in resultado.problema


def test_converter_numero_coluna_inteira_recusa_float_fracionario():
    assert converter_numero(41.6, int).problema is not None
    assert converter_numero(41.0, int).valor == 41


def test_converter_numero_ponto_com_dois_digitos_e_decimal_no_tempo():
    assert converter_numero("5.00", float).valor == pytest.approx(5.0)


def test_converter_numero_tipo_invalido_e_erro_de_programacao():
    # Nao e dado sujo, e chamada errada: quebra alto em vez de virar ocorrencia.
    with pytest.raises(ValueError, match="int ou float"):
        converter_numero("5000", str)


def test_converter_numero_negativo_passa_para_a_validacao():
    # Tempo negativo e impossivel, mas quem recusa e o Bloco 5. A limpeza so
    # converte - recusar aqui esconderia a ocorrencia do relatorio.
    assert converter_numero("-53,5", float).valor == pytest.approx(-53.5)
    assert converter_numero(-53.5, float).problema is None


@pytest.mark.parametrize("vazio", [None, "", "   "])
def test_converter_numero_vazio_nao_e_problema(vazio):
    resultado = converter_numero(vazio, int)
    assert resultado.valor is None
    assert resultado.problema is None


@pytest.mark.parametrize("entrada", ["abc", "5.000abc", "--5", "R$ 5.000"])
def test_converter_numero_invalido_diz_o_valor_e_o_que_sobrou(entrada):
    resultado = converter_numero(entrada, int)
    assert resultado.valor is None
    assert entrada in resultado.problema


def test_converter_numero_booleano_e_recusado():
    # True e instancia de int em Python: sem a guarda viraria 1 silenciosamente.
    assert converter_numero(True, int).problema is not None


def test_converter_numero_data_em_coluna_numerica_e_recusada():
    assert converter_numero(datetime(2026, 3, 5), int).problema is not None


def test_converter_numero_sem_mudanca_nao_reporta_correcao():
    assert converter_numero(5000, int).correcao is None


def test_converter_numero_texto_convertido_reporta_correcao():
    assert converter_numero("5.000", int).correcao is not None


def test_tipo_por_coluna_cobre_as_colunas_numericas_do_config():
    """
    O mapa de tipos e o nome canonico das colunas precisam andar juntos. Se
    alguem renomear 'tiragem_boa' no config sem ajustar o mapa, e aqui que cai.
    """
    cfg = carregar()
    for coluna in TIPO_NUMERICO_POR_COLUNA:
        assert coluna in cfg.colunas, f"{coluna!r} saiu da secao 'colunas' do config"
    assert TIPO_NUMERICO_POR_COLUNA["tiragem_prevista"] is int
    assert TIPO_NUMERICO_POR_COLUNA["tempo_setup_min"] is float


# ---------------------------------------------------------------------------
# Contra o config.yaml de verdade
# ---------------------------------------------------------------------------

def test_toda_variante_do_config_casa_com_seu_canonico():
    """
    O config e a fonte da verdade das variantes. Se alguem acrescentar uma
    variante que ja pertence a outra maquina, ou digitar o canonico errado, e
    aqui que aparece.
    """
    cfg = carregar()

    for canonico, variantes in cfg.maquinas.items():
        assert normalizar_maquina(canonico, cfg.maquinas).valor == canonico
        for variante in variantes:
            assert normalizar_maquina(variante, cfg.maquinas).valor == canonico

    for canonico, variantes in cfg.turnos.items():
        assert normalizar_turno(canonico, cfg.turnos).valor == canonico
        for variante in variantes:
            assert normalizar_turno(variante, cfg.turnos).valor == canonico

    for canonico, variantes in cfg.status.items():
        assert normalizar_status(canonico, cfg.status).valor == canonico
        for variante in variantes:
            assert normalizar_status(variante, cfg.status).valor == canonico
