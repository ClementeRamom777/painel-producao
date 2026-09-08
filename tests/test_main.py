"""
Testes da orquestracao.

O que importa aqui nao e o calculo - isso e dos outros arquivos de teste - e sim
o contrato do programa com quem o executa: o codigo de saida, o arquivo que
aparece em disco, e a garantia de que nenhuma excecao vira traceback na tela.

Os testes rodam sobre uma copia do config apontando para tmp_path, com um
volume pequeno de dados. Nenhum toca a pasta real do projeto.
"""

import csv
import logging
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.config import CODIGO_SAIDA_ERRO_CONFIG, CONFIG_PADRAO
from src.main import CODIGO_ERRO, CODIGO_SUCESSO, construir_analisador, main
from src.validacao import COLUNAS_QUALIDADE


@pytest.fixture
def projeto(tmp_path):
    """
    Um projeto completo em tmp_path: config proprio, pastas proprias.

    O config real e copiado e so tem os caminhos trocados, para os testes
    exercitarem a configuracao de verdade - metas, sinonimos e limites - em vez
    de uma versao reduzida que poderia divergir dela.
    """
    dados = yaml.safe_load(CONFIG_PADRAO.read_text(encoding="utf-8"))
    dados["caminhos"] = {
        "entrada": "dados/entrada",
        "banco": "dados/producao.db",
        "saida": "saida",
        "logs": "logs",
    }
    # Volume pequeno: o teste conferе fluxo, nao estatistica.
    dados["gerador"]["meses"] = 2
    dados["gerador"]["arquivos"] = 1
    dados["gerador"]["ordens_por_mes"] = 25

    caminho = tmp_path / "config.yaml"
    caminho.write_text(yaml.safe_dump(dados, allow_unicode=True), encoding="utf-8")
    return caminho


@pytest.fixture(autouse=True)
def log_limpo():
    """
    Desliga os handlers que main() instalou, ao fim de cada teste.

    Sem isso o FileHandler segura o arquivo de log aberto e o Windows recusa
    apagar o tmp_path do teste seguinte.
    """
    yield
    raiz = logging.getLogger()
    for handler in list(raiz.handlers):
        raiz.removeHandler(handler)
        handler.close()


def rodar(projeto, *argumentos) -> int:
    """Executa main() com o config do projeto de teste."""
    return main([*argumentos, "--config", str(projeto)])


# ---------------------------------------------------------------------------
# Codigos de saida
# ---------------------------------------------------------------------------

def test_sem_subcomando_mostra_ajuda_e_falha(projeto, capsys):
    assert main(["--config", str(projeto)]) == CODIGO_ERRO
    assert "COMANDO" in capsys.readouterr().out


def test_config_invalido_sai_com_dois(tmp_path, capsys):
    quebrado = tmp_path / "config.yaml"
    quebrado.write_text("caminhos:\n  entrada: x\n", encoding="utf-8")
    assert main(["indicadores", "--config", str(quebrado)]) == CODIGO_SAIDA_ERRO_CONFIG
    assert "ERRO DE CONFIGURACAO" in capsys.readouterr().err


def test_config_inexistente_sai_com_dois(tmp_path):
    ausente = tmp_path / "nao_existe.yaml"
    assert main(["painel", "--config", str(ausente)]) == CODIGO_SAIDA_ERRO_CONFIG


def test_erro_de_execucao_sai_com_um_e_sem_traceback(projeto, capsys):
    # Banco ainda nao existe: erro esperado, mensagem legivel.
    assert rodar(projeto, "indicadores") == CODIGO_ERRO
    saida = capsys.readouterr()
    assert saida.err.startswith("ERRO:") or "ERRO:" in saida.err
    assert "Traceback" not in saida.err
    assert "Traceback" not in saida.out


def test_data_invalida_e_recusada_antes_da_consulta(projeto):
    assert rodar(projeto, "gerar-dados") == CODIGO_SUCESSO
    assert rodar(projeto, "importar") == CODIGO_SUCESSO
    assert rodar(projeto, "indicadores", "--de", "31/02/2026") == CODIGO_ERRO


def test_excecao_inesperada_vira_linha_legivel(projeto, capsys, monkeypatch):
    # A rede de seguranca do topo: qualquer coisa que escape das excecoes
    # conhecidas ainda precisa sair com codigo 1 e apontar o log.
    import src.main as modulo

    def explodir(*_, **__):
        raise RuntimeError("falha nao prevista")

    monkeypatch.setitem(modulo.COMANDOS, "painel", explodir)
    assert rodar(projeto, "painel") == CODIGO_ERRO

    saida = capsys.readouterr()
    assert "ERRO INESPERADO" in saida.err
    assert "falha nao prevista" in saida.err
    assert "execucao.log" in saida.err
    assert "Traceback" not in saida.err


def test_a_pilha_da_excecao_inesperada_vai_para_o_log(projeto, monkeypatch):
    import src.main as modulo

    def explodir(*_, **__):
        raise RuntimeError("falha nao prevista")

    monkeypatch.setitem(modulo.COMANDOS, "painel", explodir)
    rodar(projeto, "painel")

    log = (projeto.parent / "logs" / "execucao.log").read_text(encoding="utf-8")
    assert "Traceback" in log
    assert "falha nao prevista" in log


# ---------------------------------------------------------------------------
# A trava da Decisao 2
# ---------------------------------------------------------------------------

def test_rejeicao_acima_do_limite_sai_com_um(projeto, capsys):
    assert rodar(projeto, "gerar-dados") == CODIGO_SUCESSO

    dados = yaml.safe_load(projeto.read_text(encoding="utf-8"))
    dados["validacao"]["taxa_rejeicao_limite"] = 0.0
    projeto.write_text(yaml.safe_dump(dados, allow_unicode=True), encoding="utf-8")

    assert rodar(projeto, "importar") == CODIGO_ERRO
    assert "passa do limite" in capsys.readouterr().err


def test_rejeicao_acima_do_limite_nao_impede_a_gravacao(projeto):
    # As linhas aprovadas continuam sendo dado bom. Abortar puniria duas vezes.
    rodar(projeto, "gerar-dados")
    dados = yaml.safe_load(projeto.read_text(encoding="utf-8"))
    dados["validacao"]["taxa_rejeicao_limite"] = 0.0
    projeto.write_text(yaml.safe_dump(dados, allow_unicode=True), encoding="utf-8")

    assert rodar(projeto, "importar") == CODIGO_ERRO
    assert (projeto.parent / "dados" / "producao.db").is_file()


def test_tudo_completa_as_saidas_mesmo_com_rejeicao_alta(projeto):
    # O painel gerado mostra a taxa alta no rodape: e a tela que alguem precisa
    # ver quando a entrada degradou.
    dados = yaml.safe_load(projeto.read_text(encoding="utf-8"))
    dados["validacao"]["taxa_rejeicao_limite"] = 0.0
    projeto.write_text(yaml.safe_dump(dados, allow_unicode=True), encoding="utf-8")

    assert rodar(projeto, "tudo") == CODIGO_ERRO
    saida = projeto.parent / "saida"
    assert (saida / "painel.html").is_file()
    assert (saida / "relatorio_producao.xlsx").is_file()
    assert (saida / "relatorio_qualidade.csv").is_file()


# ---------------------------------------------------------------------------
# 'tudo' e as saidas da secao 6
# ---------------------------------------------------------------------------

def test_tudo_produz_as_quatro_saidas_num_comando(projeto):
    assert rodar(projeto, "tudo") == CODIGO_SUCESSO

    raiz = projeto.parent
    assert (raiz / "saida" / "painel.html").is_file()
    assert (raiz / "saida" / "relatorio_producao.xlsx").is_file()
    assert (raiz / "saida" / "relatorio_qualidade.csv").is_file()
    assert (raiz / "logs" / "execucao.log").is_file()
    assert (raiz / "dados" / "producao.db").is_file()


def test_tudo_roda_duas_vezes_sem_duplicar(projeto):
    import sqlite3

    assert rodar(projeto, "tudo") == CODIGO_SUCESSO
    banco = projeto.parent / "dados" / "producao.db"
    with sqlite3.connect(banco) as con:
        primeira = con.execute("SELECT COUNT(*) FROM ordem_producao").fetchone()[0]

    assert rodar(projeto, "tudo") == CODIGO_SUCESSO
    with sqlite3.connect(banco) as con:
        segunda = con.execute("SELECT COUNT(*) FROM ordem_producao").fetchone()[0]
        execucoes = con.execute(
            "SELECT COUNT(*) FROM execucao_importacao"
        ).fetchone()[0]

    assert primeira == segunda
    assert execucoes == 2  # so o historico de importacoes cresce


# ---------------------------------------------------------------------------
# Relatorio de qualidade da secao 6.3
# ---------------------------------------------------------------------------

def test_relatorio_de_qualidade_tem_as_colunas_da_secao_6_3(projeto):
    rodar(projeto, "gerar-dados")
    rodar(projeto, "importar")

    destino = projeto.parent / "saida" / "relatorio_qualidade.csv"
    with destino.open(encoding="utf-8-sig", newline="") as arquivo:
        leitor = csv.DictReader(arquivo, delimiter=";")
        assert leitor.fieldnames == list(COLUNAS_QUALIDADE)
        linhas = list(leitor)

    assert linhas
    assert {linha["acao"] for linha in linhas} <= {
        "corrigido", "rejeitado", "descartado", "duplicado",
    }


def test_toda_ocorrencia_aponta_arquivo_e_linha(projeto):
    # E o contrato da Decisao 2: nenhuma linha some sem deixar rastro, e o
    # rastro precisa dizer ONDE.
    rodar(projeto, "gerar-dados")
    rodar(projeto, "importar")

    destino = projeto.parent / "saida" / "relatorio_qualidade.csv"
    with destino.open(encoding="utf-8-sig", newline="") as arquivo:
        for linha in csv.DictReader(arquivo, delimiter=";"):
            assert linha["arquivo"].endswith(".xlsx")
            assert linha["linha_planilha"].isdigit()
            assert linha["problema"]


def test_relatorio_de_qualidade_e_gravado_mesmo_sem_ocorrencia(projeto):
    # Um CSV so com cabecalho diz 'rodou e nao achou nada'. A ausencia do
    # arquivo e indistinguivel de 'nao rodou'.
    dados = yaml.safe_load(projeto.read_text(encoding="utf-8"))
    dados["gerador"]["taxa_sujeira"] = 0.0
    projeto.write_text(yaml.safe_dump(dados, allow_unicode=True), encoding="utf-8")

    rodar(projeto, "gerar-dados")
    rodar(projeto, "importar")

    destino = projeto.parent / "saida" / "relatorio_qualidade.csv"
    assert destino.is_file()
    with destino.open(encoding="utf-8-sig", newline="") as arquivo:
        linhas = list(csv.reader(arquivo, delimiter=";"))
    assert linhas[0] == list(COLUNAS_QUALIDADE)


# ---------------------------------------------------------------------------
# Log da secao 6.4
# ---------------------------------------------------------------------------

def test_log_sai_no_formato_da_secao_6_4(projeto):
    rodar(projeto, "gerar-dados")

    linhas = (projeto.parent / "logs" / "execucao.log").read_text(
        encoding="utf-8"
    ).splitlines()
    assert linhas
    # data hora | nivel | modulo | mensagem
    campos = [parte.strip() for parte in linhas[0].split("|")]
    assert len(campos) >= 4
    assert campos[1] in ("DEBUG", "INFO", "WARNING", "ERROR")
    assert campos[2]


def test_log_acumula_entre_execucoes(projeto):
    # O log e historico: abrir em modo de truncar apagaria a execucao anterior,
    # que e justamente a que se quer comparar quando algo mudou.
    rodar(projeto, "gerar-dados")
    primeiro = (projeto.parent / "logs" / "execucao.log").read_text(encoding="utf-8")
    rodar(projeto, "gerar-dados")
    segundo = (projeto.parent / "logs" / "execucao.log").read_text(encoding="utf-8")
    assert len(segundo) > len(primeiro)


def test_verbose_desce_o_nivel_do_arquivo(projeto):
    rodar(projeto, "gerar-dados", "--verbose")
    log = (projeto.parent / "logs" / "execucao.log").read_text(encoding="utf-8")
    assert "DEBUG" in log or "INFO" in log


def test_chamar_main_duas_vezes_nao_duplica_a_linha_do_log(projeto):
    # Cada chamada instala handlers; sem limpar os anteriores, a segunda
    # execucao escreveria tudo duas vezes.
    rodar(projeto, "gerar-dados")
    rodar(projeto, "gerar-dados")

    linhas = (projeto.parent / "logs" / "execucao.log").read_text(
        encoding="utf-8"
    ).splitlines()
    marcador = [linha for linha in linhas if "gerando planilhas sinteticas" in linha]
    assert len(marcador) == 2


# ---------------------------------------------------------------------------
# Contrato da linha de comando da secao 8
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "comando",
    ["gerar-dados", "importar", "indicadores", "painel", "relatorio", "tudo"],
)
def test_os_seis_subcomandos_da_secao_8_existem(comando):
    analisador = construir_analisador()
    args = analisador.parse_args([comando])
    assert args.comando == comando


@pytest.mark.parametrize(
    "comando",
    ["gerar-dados", "importar", "indicadores", "painel", "relatorio", "tudo"],
)
def test_todos_aceitam_config_e_verbose(comando):
    # A secao 8 diz 'todos aceitam --config caminho.yaml e --verbose'.
    analisador = construir_analisador()
    args = analisador.parse_args([comando, "--config", "outro.yaml", "--verbose"])
    assert args.config == "outro.yaml"
    assert args.verbose is True


def test_subcomando_desconhecido_e_recusado():
    with pytest.raises(SystemExit):
        construir_analisador().parse_args(["inventado"])
