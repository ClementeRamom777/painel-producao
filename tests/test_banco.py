"""
Testes da gravacao no SQLite.

O nucleo do bloco e a idempotencia, e ela e testada no sentido forte: nao basta
'COUNT(*) nao mudou', porque um upsert que reescreve tudo a cada rodada mantem a
contagem parada e ainda assim muda o banco. Os testes conferem que as LINHAS
ficam identicas, carimbo de importacao incluido.

O outro nucleo e a duplicata deixar rastro. Duas linhas com a mesma chave natural
colapsam numa so, e esse colapso e a falha silenciosa que o bloco existe para
nao cometer.

Cada teste abre um banco proprio em tmp_path, entao nenhum depende da ordem em
que os outros rodaram.
"""

import sqlite3
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from apoio import cfg_teste, linhas
from src.banco import (
    COLUNAS_NEGOCIO,
    COLUNAS_ORDEM,
    ErroBanco,
    conectar,
    contar_ordens,
    criar_esquema,
    gravar_ordens,
    hash_linha,
    registrar_execucao,
    sincronizar_maquinas,
)
from src.validacao import ACAO_CONFLITO, ACAO_DUPLICADO

MOMENTO = "2026-09-08T10:00:00"
OUTRO_MOMENTO = "2026-09-09T18:30:00"


@pytest.fixture
def conexao(tmp_path):
    """Um banco vazio com o esquema criado, fechado ao fim do teste."""
    con = conectar(tmp_path / "producao.db")
    criar_esquema(con)
    yield con
    con.close()


def ordens(conexao) -> list[dict]:
    """Todas as ordens do banco, em dicionario, ordenadas pela chave."""
    return [
        dict(linha)
        for linha in conexao.execute(
            "SELECT * FROM ordem_producao ORDER BY hash_linha"
        )
    ]


# ---------------------------------------------------------------------------
# Chave natural
# ---------------------------------------------------------------------------

def test_hash_e_estavel_entre_chamadas():
    primeiro = hash_linha("OS-2026-0001", date(2026, 3, 2), "Heidelberg SM52")
    segundo = hash_linha("OS-2026-0001", date(2026, 3, 2), "Heidelberg SM52")
    assert primeiro == segundo
    assert len(primeiro) == 64


@pytest.mark.parametrize(
    "numero_os, maquina",
    [
        ("os-2026-0001", "Heidelberg SM52"),
        ("  OS-2026-0001  ", "Heidelberg SM52"),
        ("OS-2026-0001", "heidelberg  sm52"),
    ],
)
def test_hash_ignora_caixa_e_espaco(numero_os, maquina):
    # O mesmo apontamento salvo duas vezes com caixa diferente precisa colidir,
    # senao entraria como duas ordens e dobraria a producao do dia.
    referencia = hash_linha("OS-2026-0001", date(2026, 3, 2), "Heidelberg SM52")
    assert hash_linha(numero_os, date(2026, 3, 2), maquina) == referencia


@pytest.mark.parametrize(
    "numero_os, data_entrada, maquina",
    [
        ("OS-2026-0002", date(2026, 3, 2), "Heidelberg SM52"),
        ("OS-2026-0001", date(2026, 3, 3), "Heidelberg SM52"),
        ("OS-2026-0001", date(2026, 3, 2), "Heidelberg SM74"),
    ],
)
def test_hash_muda_quando_qualquer_parte_da_chave_muda(numero_os, data_entrada, maquina):
    referencia = hash_linha("OS-2026-0001", date(2026, 3, 2), "Heidelberg SM52")
    assert hash_linha(numero_os, data_entrada, maquina) != referencia


def test_hash_recusa_data_que_nao_foi_convertida():
    with pytest.raises(ErroBanco, match="data_entrada"):
        hash_linha("OS-2026-0001", "02/03/2026", "Heidelberg SM52")


# ---------------------------------------------------------------------------
# Esquema
# ---------------------------------------------------------------------------

def test_criar_esquema_pode_rodar_de_novo(conexao):
    criar_esquema(conexao)
    tabelas = {
        linha["name"]
        for linha in conexao.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"maquina", "ordem_producao", "execucao_importacao"} <= tabelas


def test_chave_estrangeira_esta_ligada(conexao):
    # Sem o PRAGMA foreign_keys o REFERENCES do esquema seria decoracao e esta
    # insercao passaria.
    with pytest.raises(sqlite3.IntegrityError):
        conexao.execute(
            "INSERT INTO ordem_producao ("
            "hash_linha, numero_os, maquina_id, data_entrada, status, "
            "arquivo_origem, importado_em) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("abc", "OS-1", 999, "2026-03-02", "ENTREGUE", "p.xlsx", MOMENTO),
        )


# ---------------------------------------------------------------------------
# Maquinas
# ---------------------------------------------------------------------------

def test_sincronizar_cria_todas_as_maquinas_do_config(conexao):
    ids, criadas, fora = sincronizar_maquinas(conexao, cfg_teste())
    assert set(ids) == {"Heidelberg SM52"}
    assert criadas == ["Heidelberg SM52"]
    assert fora == []


def test_sincronizar_de_novo_nao_cria_nada(conexao):
    cfg = cfg_teste()
    sincronizar_maquinas(conexao, cfg)
    ids, criadas, _ = sincronizar_maquinas(conexao, cfg)
    assert criadas == []
    assert len(ids) == 1


def test_maquina_do_banco_fora_do_config_e_reportada_e_nao_apagada(conexao):
    conexao.execute("INSERT INTO maquina (nome_canonico) VALUES ('Roland 700')")
    ids, _, fora = sincronizar_maquinas(conexao, cfg_teste())
    assert fora == ["Roland 700"]
    assert "Roland 700" in ids


# ---------------------------------------------------------------------------
# Gravacao
# ---------------------------------------------------------------------------

def test_grava_a_ordem_aprovada(conexao):
    resultado = gravar_ordens(conexao, linhas({}), cfg_teste(), MOMENTO)

    assert resultado.inseridas == 1
    assert resultado.gravadas == 1
    assert contar_ordens(conexao) == 1

    gravada = ordens(conexao)[0]
    assert gravada["numero_os"] == "OS-2026-0001"
    assert gravada["tiragem_boa"] == 5100
    assert gravada["arquivo_origem"] == "planilha.xlsx"
    assert gravada["importado_em"] == MOMENTO


def test_data_e_gravada_como_texto_iso(conexao):
    gravar_ordens(conexao, linhas({}), cfg_teste(), MOMENTO)
    gravada = ordens(conexao)[0]
    # TEXT em YYYY-MM-DD ordena corretamente como string, que e o que permite ao
    # Bloco 7 filtrar periodo sem converter nada.
    assert gravada["data_entrada"] == "2026-03-02"
    assert gravada["data_prazo"] == "2026-03-12"
    assert isinstance(gravada["data_entrada"], str)


def test_data_de_entrega_vazia_vira_nulo(conexao):
    gravar_ordens(
        conexao,
        linhas({"status": "EM PRODUCAO", "data_entrega": None}),
        cfg_teste(),
        MOMENTO,
    )
    assert ordens(conexao)[0]["data_entrega"] is None


def test_lote_vazio_ainda_sincroniza_maquinas(conexao):
    vazio = linhas()
    resultado = gravar_ordens(conexao, vazio, cfg_teste(), MOMENTO)
    assert resultado.gravadas == 0
    assert resultado.maquinas_criadas == ["Heidelberg SM52"]


def test_maquina_que_o_config_nao_declara_recusa(conexao):
    # O lote foi limpo contra um config e gravado contra outro. A maquina da
    # linha nao tem id, e sem a guarda a chave estrangeira apontaria para o nada
    # ou o codigo quebraria com um KeyError sem contexto.
    outro_config = cfg_teste(
        maquinas={"Roland 700": ["roland"]},
        metas_produtividade={"Roland 700": 8000},
    )
    with pytest.raises(ErroBanco, match="Heidelberg SM52"):
        gravar_ordens(conexao, linhas({}), outro_config, MOMENTO)


# ---------------------------------------------------------------------------
# Idempotencia
# ---------------------------------------------------------------------------

def test_gravar_duas_vezes_nao_duplica(conexao):
    cfg = cfg_teste()
    gravar_ordens(conexao, linhas({}), cfg, MOMENTO)
    segundo = gravar_ordens(conexao, linhas({}), cfg, OUTRO_MOMENTO)

    assert contar_ordens(conexao) == 1
    assert segundo.inseridas == 0
    assert segundo.atualizadas == 0
    assert segundo.inalteradas == 1


def test_reimportar_nao_mexe_na_linha(conexao):
    # O teste forte da idempotencia: nao basta a contagem parar de punho, a
    # linha inteira precisa ficar identica - inclusive 'importado_em', que sem a
    # clausula WHERE do upsert seria reescrito a cada rodada.
    cfg = cfg_teste()
    gravar_ordens(conexao, linhas({}), cfg, MOMENTO)
    antes = ordens(conexao)

    gravar_ordens(conexao, linhas({}), cfg, OUTRO_MOMENTO)

    assert ordens(conexao) == antes
    assert ordens(conexao)[0]["importado_em"] == MOMENTO


def test_planilha_corrigida_atualiza_a_ordem(conexao):
    cfg = cfg_teste()
    gravar_ordens(conexao, linhas({}), cfg, MOMENTO)
    segundo = gravar_ordens(conexao, linhas({"tiragem_boa": 4800}), cfg, OUTRO_MOMENTO)

    assert segundo.atualizadas == 1
    assert segundo.inseridas == 0
    assert contar_ordens(conexao) == 1

    gravada = ordens(conexao)[0]
    assert gravada["tiragem_boa"] == 4800
    assert gravada["importado_em"] == OUTRO_MOMENTO


def test_mudanca_so_no_arquivo_de_origem_conta_como_atualizacao(conexao):
    # A ordem migrou de arquivo: a producao e a mesma, mas a procedencia mudou e
    # o banco precisa apontar para o arquivo certo.
    cfg = cfg_teste()
    gravar_ordens(conexao, linhas({}), cfg, MOMENTO)

    lote = linhas({})
    lote.loc[0, "arquivo_origem"] = "outra_planilha.xlsx"
    segundo = gravar_ordens(conexao, lote, cfg, OUTRO_MOMENTO)

    assert segundo.atualizadas == 1
    assert ordens(conexao)[0]["arquivo_origem"] == "outra_planilha.xlsx"


def test_ordem_com_a_mesma_chave_em_caixa_diferente_nao_duplica(conexao):
    cfg = cfg_teste()
    gravar_ordens(conexao, linhas({}), cfg, MOMENTO)
    gravar_ordens(conexao, linhas({"numero_os": "os-2026-0001"}), cfg, OUTRO_MOMENTO)
    assert contar_ordens(conexao) == 1


# ---------------------------------------------------------------------------
# Duplicata dentro do lote
# ---------------------------------------------------------------------------

def test_copia_exata_no_lote_deixa_rastro(conexao):
    resultado = gravar_ordens(conexao, linhas({}, {}), cfg_teste(), MOMENTO)

    assert contar_ordens(conexao) == 1
    assert resultado.inseridas == 1
    assert resultado.duplicadas == 1
    assert resultado.conflitos == 0

    ocorrencia = resultado.ocorrencias[0]
    assert ocorrencia.acao == ACAO_DUPLICADO
    assert ocorrencia.linha_planilha == 3
    # A mensagem aponta a linha que ficou no lugar dela.
    assert "linha 2" in ocorrencia.problema


def test_a_primeira_linha_da_chave_e_a_que_fica(conexao):
    # Determinismo: a leitura entrega em ordem alfabetica por arquivo e
    # crescente por linha, entao 'primeira' e sempre a mesma escolha.
    gravar_ordens(
        conexao, linhas({"tiragem_boa": 100}, {"tiragem_boa": 200}), cfg_teste(), MOMENTO
    )
    assert ordens(conexao)[0]["tiragem_boa"] == 100


def test_mesma_chave_com_conteudo_diferente_e_conflito(conexao):
    resultado = gravar_ordens(
        conexao, linhas({}, {"tiragem_boa": 999, "refugo": 7}), cfg_teste(), MOMENTO
    )

    assert resultado.conflitos == 1
    assert resultado.duplicadas == 0

    ocorrencia = resultado.ocorrencias[0]
    # Acao propria: quem filtra o CSV por 'duplicado' nao pode receber junto o
    # caso que exige investigacao.
    assert ocorrencia.acao == ACAO_CONFLITO
    assert "conteudo diferente" in ocorrencia.problema
    assert "tiragem_boa" in ocorrencia.problema
    assert "refugo" in ocorrencia.problema


def test_conflito_e_duplicata_no_mesmo_lote_saem_com_acoes_diferentes(conexao):
    lote = linhas({}, {}, {"tiragem_boa": 999})
    resultado = gravar_ordens(conexao, lote, cfg_teste(), MOMENTO)

    assert resultado.duplicadas == 1
    assert resultado.conflitos == 1
    assert [o.acao for o in resultado.ocorrencias] == [ACAO_DUPLICADO, ACAO_CONFLITO]
    assert contar_ordens(conexao) == 1


def test_repeticao_em_arquivos_diferentes_e_duplicata_nao_conflito(conexao):
    # A mesma ordem exportada em dois arquivos do lote continua sendo repeticao.
    # Por isso 'arquivo_origem' fica fora de COLUNAS_NEGOCIO.
    lote = linhas({}, {})
    lote.loc[1, "arquivo_origem"] = "outra_planilha.xlsx"
    resultado = gravar_ordens(conexao, lote, cfg_teste(), MOMENTO)

    assert resultado.duplicadas == 1
    assert resultado.conflitos == 0


def test_arquivo_de_origem_esta_fora_das_colunas_de_negocio():
    assert "arquivo_origem" in COLUNAS_ORDEM
    assert "arquivo_origem" not in COLUNAS_NEGOCIO
    assert "importado_em" not in COLUNAS_NEGOCIO
    assert "hash_linha" not in COLUNAS_NEGOCIO


# ---------------------------------------------------------------------------
# Historico de execucoes
# ---------------------------------------------------------------------------

def test_execucao_e_registrada(conexao):
    registrar_execucao(
        conexao,
        momento=MOMENTO,
        arquivos_lidos=3,
        linhas_lidas=736,
        linhas_gravadas=678,
        linhas_corrigidas=67,
        linhas_rejeitadas=42,
        duracao_segundos=1.23456,
    )
    registro = dict(conexao.execute("SELECT * FROM execucao_importacao").fetchone())
    assert registro["executado_em"] == MOMENTO
    assert registro["linhas_lidas"] == 736
    assert registro["linhas_rejeitadas"] == 42
    assert registro["duracao_segundos"] == 1.235


def test_historico_de_execucoes_cresce_a_cada_rodada(conexao):
    # E a unica coisa que muda quando se reimporta uma pasta inalterada, e e o
    # que permite ver a entrada degradando ao longo do tempo.
    for _ in range(3):
        registrar_execucao(
            conexao, momento=MOMENTO, arquivos_lidos=1, linhas_lidas=1,
            linhas_gravadas=1, linhas_corrigidas=0, linhas_rejeitadas=0,
            duracao_segundos=0.1,
        )
    total = conexao.execute("SELECT COUNT(*) AS t FROM execucao_importacao").fetchone()
    assert total["t"] == 3
