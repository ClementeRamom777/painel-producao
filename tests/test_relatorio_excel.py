"""
Testes do relatorio Excel.

O criterio de aceite do bloco - 'abre no Excel sem aviso de reparo' - nao da
para verificar sem o Excel, mas da para verificar as causas dele: nome de aba
invalido, caractere de controle em celula, XML mal formado no pacote. Sao esses
os testes que importam aqui, mais a garantia de que numero foi gravado como
numero e nao como texto formatado.
"""

import re
import sys
import zipfile
from datetime import date, datetime
from pathlib import Path
from xml.etree import ElementTree

import pytest
from openpyxl import load_workbook

sys.path.insert(0, str(Path(__file__).resolve().parent))

from apoio import cfg_teste, linhas
from src.banco import conectar, criar_esquema, gravar_ordens
from src.indicadores import calcular, calcular_corte
from src.limpeza import STATUS_CANCELADA
from src.painel import ordens_atrasadas
from src.relatorio_excel import (
    ABAS_ESPERADAS,
    FORMATO_DATA,
    FORMATO_GERAL,
    FORMATO_INTEIRO,
    FORMATO_PERCENTUAL,
    FORMATO_UM_DECIMAL,
    ErroRelatorio,
    _mascara,
    _texto_seguro,
    gerar,
    montar_livro,
)
from src.validacao import ACAO_CORRIGIDO, ACAO_REJEITADO, Ocorrencia

MOMENTO = "2026-09-08T10:00:00"

OCORRENCIAS = [
    Ocorrencia(
        arquivo="planilha.xlsx", linha_planilha=4, coluna="maquina",
        valor_original="KOMORI 28", problema="reconhecido como Komori Lithrone 28",
        acao=ACAO_CORRIGIDO, valor_final="Komori Lithrone 28",
    ),
    Ocorrencia(
        arquivo="planilha.xlsx", linha_planilha=9, coluna="numero_os",
        valor_original="", problema="campo obrigatorio vazio",
        acao=ACAO_REJEITADO, valor_final="",
    ),
]


@pytest.fixture
def banco(tmp_path):
    con = conectar(tmp_path / "producao.db")
    criar_esquema(con)
    yield con
    con.close()


def livro_de(banco, *alteracoes, ocorrencias=OCORRENCIAS, cfg=None):
    """Monta o Workbook sobre as linhas dadas."""
    cfg = cfg_teste() if cfg is None else cfg
    gravar_ordens(banco, linhas(*alteracoes), cfg, MOMENTO)
    banco.commit()
    resultado = calcular(banco)
    return montar_livro(
        resultado,
        calcular_corte(banco, "cliente", resultado.de, resultado.ate),
        ordens_atrasadas(banco, resultado, limite=None),
        ocorrencias,
        cfg,
    )


def gravado(banco, tmp_path, *alteracoes, **extras):
    """Grava o livro em disco e devolve o caminho e o livro relido."""
    destino = tmp_path / "relatorio.xlsx"
    livro_de(banco, *alteracoes, **extras).save(destino)
    return destino, load_workbook(destino)


# ---------------------------------------------------------------------------
# Estrutura do arquivo: as causas do aviso de reparo
# ---------------------------------------------------------------------------

def test_o_pacote_e_um_zip_integro_com_xml_valido(banco, tmp_path):
    destino, _ = gravado(banco, tmp_path, {})
    with zipfile.ZipFile(destino) as pacote:
        assert pacote.testzip() is None
        for nome in pacote.namelist():
            if nome.endswith((".xml", ".rels")):
                ElementTree.fromstring(pacote.read(nome))  # levanta se invalido


def test_as_cinco_abas_na_ordem_da_especificacao(banco, tmp_path):
    _, livro = gravado(banco, tmp_path, {})
    assert livro.sheetnames == list(ABAS_ESPERADAS)


def test_nome_de_aba_cabe_no_limite_do_excel(banco, tmp_path):
    # O Excel recusa nome com mais de 31 caracteres ou com [ ] : * ? / \ e
    # abre o arquivo com aviso de reparo.
    _, livro = gravado(banco, tmp_path, {})
    for nome in livro.sheetnames:
        assert len(nome) <= 31, nome
        assert not re.search(r"[\[\]:*?/\\]", nome), nome


def test_nao_sobra_a_aba_vazia_que_o_workbook_cria(banco, tmp_path):
    _, livro = gravado(banco, tmp_path, {})
    assert "Sheet" not in livro.sheetnames


def test_caractere_de_controle_e_removido_antes_de_gravar(banco, tmp_path):
    # 'valor_original' carrega o conteudo cru da planilha de origem: pode vir
    # qualquer coisa, e o openpyxl levanta excecao ao encontrar controle.
    suja = Ocorrencia(
        arquivo="planilha.xlsx", linha_planilha=7, coluna="produto",
        valor_original="Folder\x00A4\x0bcouche", problema="valor com controle",
        acao=ACAO_CORRIGIDO, valor_final="Folder A4",
    )
    destino, livro = gravado(banco, tmp_path, {}, ocorrencias=[suja])
    aba = livro["Qualidade dos dados"]
    assert aba.cell(2, 4).value == "FolderA4couche"


def test_texto_seguro_corta_no_limite_da_celula():
    enorme = "x" * 40_000
    assert len(_texto_seguro(enorme)) <= 32_000
    assert _texto_seguro(enorme).endswith("...")


def test_texto_seguro_de_vazio_e_string_vazia():
    assert _texto_seguro(None) == ""


# ---------------------------------------------------------------------------
# Formatacao exigida pela secao 6.2
# ---------------------------------------------------------------------------

def test_toda_aba_tem_o_painel_congelado(banco, tmp_path):
    _, livro = gravado(banco, tmp_path, {})
    for aba in livro.worksheets:
        assert aba.freeze_panes == "A2", aba.title


def test_toda_aba_tem_cabecalho_formatado(banco, tmp_path):
    _, livro = gravado(banco, tmp_path, {})
    for aba in livro.worksheets:
        cabecalho = aba.cell(1, 1)
        assert cabecalho.font.bold, aba.title
        assert cabecalho.fill.fgColor.rgb not in (None, "00000000"), aba.title


def test_toda_aba_tem_largura_de_coluna_ajustada(banco, tmp_path):
    _, livro = gravado(banco, tmp_path, {})
    for aba in livro.worksheets:
        assert len(aba.column_dimensions) >= aba.max_column, aba.title


def test_as_tabelas_tem_filtro_e_o_resumo_nao(banco, tmp_path):
    # O Resumo tem linhas em branco separando secoes: filtrar por cima delas
    # esconderia blocos inteiros sem aviso.
    _, livro = gravado(banco, tmp_path, {})
    assert livro["Resumo"].auto_filter.ref is None
    for nome in ("Por máquina", "Por cliente", "Atrasos", "Qualidade dos dados"):
        assert livro[nome].auto_filter.ref is not None, nome


# ---------------------------------------------------------------------------
# Numero e numero, nao texto
# ---------------------------------------------------------------------------

def test_indicador_e_gravado_como_numero(banco, tmp_path):
    _, livro = gravado(banco, tmp_path, {})
    aba = livro["Por máquina"]
    # Colunas B a M: tudo numero, menos 'Situação'.
    for linha in aba.iter_rows(min_row=2, max_row=aba.max_row, min_col=2, max_col=9):
        for celula in linha:
            assert not isinstance(celula.value, str), celula.coordinate


def test_indicador_sem_dado_fica_com_a_celula_vazia(banco, tmp_path):
    # Texto 'sem dado' no meio de coluna numerica quebraria MEDIA e ordenacao.
    _, livro = gravado(
        banco, tmp_path,
        {
            "status": STATUS_CANCELADA, "data_entrega": None,
            "tiragem_boa": 0, "refugo": 0, "tempo_producao_min": 0.0,
        },
    )
    aba = livro["Por máquina"]
    cabecalho = [c.value for c in aba[1]]
    coluna_folhas = cabecalho.index("Folhas/hora") + 1
    assert aba.cell(2, coluna_folhas).value is None


def test_data_e_gravada_como_data(banco, tmp_path):
    # Texto ISO ordenaria certo por acaso, mas nao filtraria como data nem
    # entraria numa conta de dias.
    #
    # O Excel nao tem tipo so-data, entao o openpyxl rele como datetime a
    # meia-noite. O que importa e nao ser texto e levar mascara de data.
    _, livro = gravado(banco, tmp_path, {"data_entrega": date(2026, 3, 20)})
    aba = livro["Atrasos"]
    prazo, entrega = aba.cell(2, 5), aba.cell(2, 6)
    assert prazo.value == datetime(2026, 3, 12)
    assert entrega.value == datetime(2026, 3, 20)
    assert "DD/MM" in prazo.number_format
    assert "DD/MM" in entrega.number_format


def test_numero_leva_formato_de_exibicao(banco, tmp_path):
    _, livro = gravado(banco, tmp_path, {})
    aba = livro["Por máquina"]
    assert aba.cell(2, 3).number_format == "#,##0"


# ---------------------------------------------------------------------------
# A mascara de exibicao
#
# Camada separada do valor: a celula pode guardar 678 corretamente e ainda assim
# a coluna sair com virgula sobrando na tela. No codigo de formato do Excel o
# '.' e o separador decimal interno - o Excel em portugues desenha virgula - e
# o '#' depois dele e placeholder opcional, que some sem levar o separador
# junto. '#,##0.##' com valor 678 mostra '678,'.
# ---------------------------------------------------------------------------

def separador_sobrando(mascara: str, valor) -> bool:
    """Diz se o Excel desenharia o separador decimal sem digito depois dele."""
    if isinstance(valor, bool) or not isinstance(valor, (int, float)):
        return False
    secao = mascara.split(";")[0]          # a primeira secao e a dos positivos
    if "." not in secao:
        return False
    depois = secao.split(".", 1)[1]
    if not depois:
        return True                        # mascara terminada no separador
    if "0" in depois:
        return False                       # casa obrigatoria: sempre sai digito
    if any(caractere != "#" for caractere in depois):
        return False                       # nao e mascara numerica simples
    return float(valor).is_integer()


@pytest.mark.parametrize(
    "mascara, valor, sobra",
    [
        ("#,##0.##", 678, True),        # o defeito: inteiro com casas opcionais
        ("#,##0.##", 678.0, True),      # float sem parte decimal e o mesmo caso
        ("#,##0.##", 3.18, False),      # com decimal a mascara preenche
        ("#,##0", 678, False),
        ("#,##0.0", 678, False),        # casa obrigatoria sai como '678,0'
        ("#,##0.00", 3.0, False),
        ("DD/MM/YYYY", 45000, False),
        ("#,##0.", 678, True),          # mascara terminada no proprio separador
    ],
)
def test_a_regra_do_separador_sobrando(mascara, valor, sobra):
    assert separador_sobrando(mascara, valor) is sobra


def test_nenhuma_celula_de_nenhuma_aba_sai_com_separador_sobrando(banco, tmp_path):
    # O teste que faltava. Os outros conferem o TIPO do valor, e o valor sempre
    # esteve certo - a mascara e outra camada e passou inteira por eles.
    _, livro = gravado(
        banco, tmp_path,
        {},
        {"numero_os": "OS-2026-0002", "data_entrega": date(2026, 3, 20)},
    )
    ruins = []
    for aba in livro.worksheets:
        for linha in aba.iter_rows(min_row=2):
            for celula in linha:
                if separador_sobrando(celula.number_format, celula.value):
                    ruins.append(
                        f"{aba.title}!{celula.coordinate} "
                        f"valor={celula.value!r} mascara={celula.number_format!r}"
                    )
    assert ruins == []


def test_valor_inteiro_na_coluna_de_casas_opcionais_perde_as_casas(banco, tmp_path):
    # 'Ordens no período' e contagem: sai '678', nunca '678,'.
    _, livro = gravado(banco, tmp_path, {})
    aba = livro["Resumo"]
    rotulos = [c.value for c in aba["A"]]
    celula = aba.cell(rotulos.index("Ordens no período") + 1, 2)
    assert celula.value == 1
    assert celula.number_format == "#,##0"


def test_valor_com_decimal_na_mesma_coluna_mantem_as_casas(banco, tmp_path):
    # A troca e por celula, nao por coluna: na mesma coluna Valor, a contagem
    # inteira perde as casas e o tempo com decimal as mantem.
    _, livro = gravado(banco, tmp_path, {"tempo_producao_min": 34.5})
    aba = livro["Resumo"]
    rotulos = [c.value for c in aba["A"]]
    celula = aba.cell(rotulos.index("Tempo de produção (min)") + 1, 2)
    assert celula.value == 34.5
    assert celula.number_format == "#,##0.##"


@pytest.mark.parametrize(
    "formato",
    [FORMATO_INTEIRO, FORMATO_UM_DECIMAL, FORMATO_PERCENTUAL, FORMATO_DATA],
)
def test_as_mascaras_fixas_nao_dependem_do_valor(formato):
    # So FORMATO_GERAL e escolhida por celula. Se outra mascara ganhar casas
    # opcionais um dia, ela precisa passar por _mascara tambem.
    assert _mascara(formato, 678) == formato
    assert _mascara(formato, 3.18) == formato


def test_mascara_de_casas_opcionais_e_trocada_so_quando_o_valor_e_inteiro():
    assert _mascara(FORMATO_GERAL, 678) == FORMATO_INTEIRO
    assert _mascara(FORMATO_GERAL, 678.0) == FORMATO_INTEIRO
    assert _mascara(FORMATO_GERAL, 3.18) == FORMATO_GERAL
    assert _mascara(FORMATO_GERAL, "texto") == FORMATO_GERAL
    assert _mascara(None, 678) is None


# ---------------------------------------------------------------------------
# Conteudo das abas
# ---------------------------------------------------------------------------

def test_atrasos_traz_todas_as_ordens_e_nao_so_as_do_painel(banco, tmp_path):
    # O painel mostra as dez primeiras; a planilha e para filtrar em cima.
    atrasadas = [
        {"numero_os": f"OS-2026-{n:04d}", "data_entrega": date(2026, 3, 13 + n)}
        for n in range(1, 13)
    ]
    _, livro = gravado(banco, tmp_path, *atrasadas)
    assert livro["Atrasos"].max_row == 13  # cabecalho + 12


def test_ordem_no_prazo_nao_entra_na_aba_de_atrasos(banco, tmp_path):
    _, livro = gravado(banco, tmp_path, {})  # entrega 10/03, prazo 12/03
    assert livro["Atrasos"].max_row == 1


def test_aba_de_qualidade_tem_as_colunas_da_secao_6_3(banco, tmp_path):
    _, livro = gravado(banco, tmp_path, {})
    aba = livro["Qualidade dos dados"]
    assert [c.value for c in aba[1]] == [
        "Arquivo", "Linha", "Coluna", "Valor original", "Problema", "Ação", "Valor final",
    ]
    assert aba.max_row == 1 + len(OCORRENCIAS)
    assert aba.cell(2, 6).value == ACAO_CORRIGIDO


def test_por_cliente_ordena_do_maior_volume_para_o_menor(banco, tmp_path):
    _, livro = gravado(
        banco, tmp_path,
        {"cliente": "CLI-001", "tiragem_boa": 1000},
        {"numero_os": "OS-2026-0002", "cliente": "CLI-002", "tiragem_boa": 9000},
    )
    aba = livro["Por cliente"]
    assert [aba.cell(linha, 1).value for linha in (2, 3)] == ["CLI-002", "CLI-001"]


def test_resumo_traz_as_somas_ao_lado_dos_indicadores(banco, tmp_path):
    # Ter os dois na mesma aba e o que permite refazer a conta sem sair dela.
    _, livro = gravado(banco, tmp_path, {})
    rotulos = [c.value for c in livro["Resumo"]["A"]]
    for esperado in (
        "Tiragem boa (folhas)", "Refugo (folhas)",
        "Tempo de produção (min)", "Tempo de acerto (min)",
    ):
        assert esperado in rotulos


def test_resumo_avisa_sobre_o_periodo_incompleto(banco, tmp_path):
    _, livro = gravado(banco, tmp_path, {})
    texto = " ".join(
        str(c.value) for linha in livro["Resumo"].iter_rows() for c in linha if c.value
    )
    assert "censura à direita" in texto
    assert "otimista" in texto


# ---------------------------------------------------------------------------
# Geracao
# ---------------------------------------------------------------------------

def test_gerar_sem_banco_recusa(tmp_path):
    with pytest.raises(ErroRelatorio, match="banco nao encontrado"):
        gerar(
            cfg_teste(),
            banco=tmp_path / "nao_existe.db",
            destino=tmp_path / "r.xlsx",
            ocorrencias=[],
        )


def test_gerar_grava_o_arquivo(banco, tmp_path):
    gravar_ordens(banco, linhas({}), cfg_teste(), MOMENTO)
    banco.commit()
    destino = tmp_path / "saida" / "relatorio.xlsx"
    caminho, ordens = gerar(
        cfg_teste(),
        banco=tmp_path / "producao.db",
        destino=destino,
        ocorrencias=OCORRENCIAS,
    )
    assert caminho == destino
    assert destino.is_file()
    assert ordens == 1
