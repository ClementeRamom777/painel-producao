"""
Gravacao das ordens aprovadas no SQLite, de forma idempotente.

Decisao 3 da especificacao: cada ordem carrega um hash_linha, SHA-256 da chave
natural normalizada, e o INSERT usa ON CONFLICT DO UPDATE. Reprocessar a mesma
pasta dez vezes produz exatamente o mesmo banco - nao so a mesma contagem de
linhas, mas as mesmas linhas, com o mesmo carimbo de importacao.

Esse 'exatamente' custa uma clausula. O upsert so reescreve a linha quando algum
campo de conteudo mudou de verdade; sem essa guarda, 'importado_em' seria
reescrito a cada execucao e o banco mudaria sozinho a cada rodada, o que
esvaziaria a promessa de idempotencia mesmo com a contagem de linhas parada.

A duplicata deixa rastro. Duas linhas com a mesma chave natural colapsam numa
so, e esse colapso e exatamente o tipo de perda silenciosa que a Decisao 2
existe para impedir: a segunda linha sai no relatorio de qualidade apontando a
linha da planilha que ficou no lugar dela.

Sao duas acoes diferentes, e a distincao e o que da para investigar:

- 'duplicado' e benigno. As duas linhas dizem a mesma coisa, o colapso nao perde
  nada e nao ha o que fazer. E o caso das dezesseis copias exatas do gerador.
- 'conflito' e problema de integridade. As duas linhas discordam, entao sao
  duas versoes da mesma ordem e o sistema escolheu uma sozinho. O relatorio
  nomeia as colunas que divergem, para alguem poder ir olhar a planilha.

Juntar os dois numa acao so faria o filtro do CSV misturar o caso raro que exige
investigacao com o caso comum que nao exige nada.

Reimportar uma ordem que ja esta no banco NAO e duplicata: e a carga incremental
funcionando. A planilha corrigida chega, a linha e atualizada, e a contagem de
atualizadas aparece no resumo da execucao.
"""

import argparse
import hashlib
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from src.config import (
    CODIGO_SAIDA_ERRO_CONFIG,
    CONFIG_PADRAO,
    Configuracao,
    ErroConfiguracao,
    carregar,
)
from src.leitura import COLUNA_ARQUIVO, COLUNA_LINHA, ErroLeitura, ler_pasta
from src.limpeza import chave_comparacao
from src.validacao import (
    ACAO_CONFLITO,
    ACAO_DUPLICADO,
    ErroValidacao,
    Ocorrencia,
    ResultadoValidacao,
    ocorrencias_de_descartes,
    validar,
)

# Esquema da secao 4.3 da especificacao, copiado sem alteracao.
#
# Data como TEXT porque o SQLite nao tem tipo DATE. O padrao da propria
# ferramenta e texto no formato YYYY-MM-DD, que ordena corretamente como string -
# e o que permite ao Bloco 7 filtrar periodo com BETWEEN sem converter nada.
ESQUEMA = """
CREATE TABLE IF NOT EXISTS maquina (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    nome_canonico  TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS ordem_producao (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    hash_linha         TEXT    NOT NULL UNIQUE,
    numero_os          TEXT    NOT NULL,
    cliente            TEXT,
    produto            TEXT,
    maquina_id         INTEGER NOT NULL REFERENCES maquina(id),
    operador           TEXT,
    turno              TEXT,
    data_entrada       TEXT    NOT NULL,
    data_prazo         TEXT,
    data_entrega       TEXT,
    status             TEXT    NOT NULL,
    tiragem_prevista   INTEGER,
    tiragem_boa        INTEGER,
    refugo             INTEGER,
    tempo_setup_min    REAL,
    tempo_producao_min REAL,
    arquivo_origem     TEXT    NOT NULL,
    importado_em       TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ordem_data    ON ordem_producao(data_entrada);
CREATE INDEX IF NOT EXISTS idx_ordem_maquina ON ordem_producao(maquina_id);

CREATE TABLE IF NOT EXISTS execucao_importacao (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    executado_em      TEXT    NOT NULL,
    arquivos_lidos    INTEGER NOT NULL,
    linhas_lidas      INTEGER NOT NULL,
    linhas_gravadas   INTEGER NOT NULL,
    linhas_corrigidas INTEGER NOT NULL,
    linhas_rejeitadas INTEGER NOT NULL,
    duracao_segundos  REAL    NOT NULL
);
"""

# Colunas de ordem_producao que a gravacao preenche, na ordem dos parametros.
COLUNAS_ORDEM = (
    "hash_linha",
    "numero_os",
    "cliente",
    "produto",
    "maquina_id",
    "operador",
    "turno",
    "data_entrada",
    "data_prazo",
    "data_entrega",
    "status",
    "tiragem_prevista",
    "tiragem_boa",
    "refugo",
    "tempo_setup_min",
    "tempo_producao_min",
    "arquivo_origem",
    "importado_em",
)

# O que o upsert reescreve e compara quando a linha ja existe. 'hash_linha' fica
# de fora por ser a chave do conflito, e 'importado_em' porque ele entra no SET
# mas nunca na comparacao: ele muda a cada execucao por definicao, e compara-lo
# faria toda reimportacao parecer alteracao.
COLUNAS_CONTEUDO = tuple(
    coluna for coluna in COLUNAS_ORDEM if coluna not in ("hash_linha", "importado_em")
)

# O que decide se duas linhas da MESMA importacao sao copia ou divergencia.
# 'arquivo_origem' fica de fora: a mesma ordem repetida em dois arquivos do lote
# continua sendo repeticao, nao duas versoes diferentes da ordem.
COLUNAS_NEGOCIO = tuple(
    coluna for coluna in COLUNAS_CONTEUDO if coluna != COLUNA_ARQUIVO
)

# Separador da chave natural antes do hash. O 0x1F e o 'unit separator' do ASCII
# e nao sobrevive a limpeza de texto, entao nao aparece em celula de planilha.
# Sem um separador assim, ('OS-1', '2026-01-02') e ('OS-12', '026-01-02')
# concatenariam na mesma string e colidiriam no hash.
SEPARADOR_CHAVE = "\x1f"

# Quantos hashes por consulta ao conferir o que ja esta no banco. O SQLite tem
# teto para o numero de parametros de uma instrucao, e carregar a tabela inteira
# em memoria so para comparar seria justamente o que a secao 11 diz que quebra
# quando o volume cresce.
TAMANHO_DO_LOTE = 500

PROBLEMA_DUPLICADA = "linha duplicada: mesma chave natural da linha {linha}"
PROBLEMA_CONFLITO = (
    "mesma chave natural da linha {linha}, com conteudo diferente em {colunas}"
)


class ErroBanco(Exception):
    """Banco inacessivel, esquema incompativel ou dado que nao pode ser gravado."""


@dataclass
class ResultadoGravacao:
    """O que a gravacao fez, separado por desfecho."""

    inseridas: int = 0
    atualizadas: int = 0
    inalteradas: int = 0
    duplicadas: int = 0
    conflitos: int = 0
    maquinas_criadas: list[str] = field(default_factory=list)
    maquinas_fora_do_config: list[str] = field(default_factory=list)
    ocorrencias: list[Ocorrencia] = field(default_factory=list)

    @property
    def gravadas(self) -> int:
        """Ordens distintas que este lote deixou no banco."""
        return self.inseridas + self.atualizadas + self.inalteradas


# ---------------------------------------------------------------------------
# Conexao e esquema
# ---------------------------------------------------------------------------

def conectar(caminho: Path) -> sqlite3.Connection:
    """
    Abre o banco, criando a pasta se ela nao existir.

    Liga as chaves estrangeiras explicitamente. O SQLite as desliga por padrao,
    por compatibilidade com bancos antigos: sem o PRAGMA, o
    'REFERENCES maquina(id)' do esquema e decoracao, e uma ordem apontando para
    maquina inexistente seria gravada sem reclamacao.
    """
    caminho = Path(caminho)
    try:
        caminho.parent.mkdir(parents=True, exist_ok=True)
        conexao = sqlite3.connect(caminho)
    except (OSError, sqlite3.Error) as erro:
        raise ErroBanco(f"nao foi possivel abrir o banco {caminho}: {erro}") from erro

    conexao.row_factory = sqlite3.Row
    conexao.execute("PRAGMA foreign_keys = ON")
    return conexao


def criar_esquema(conexao: sqlite3.Connection) -> None:
    """Cria tabelas e indices que ainda nao existem."""
    try:
        conexao.executescript(ESQUEMA)
    except sqlite3.Error as erro:
        raise ErroBanco(f"nao foi possivel criar o esquema: {erro}") from erro


# ---------------------------------------------------------------------------
# Chave natural
# ---------------------------------------------------------------------------

def hash_linha(numero_os: str, data_entrada: date, maquina: str) -> str:
    """
    SHA-256 da chave natural normalizada da ordem.

    A chave e numero da OS, data de entrada e maquina - os tres campos de
    'validacao.obrigatorios', que e o que garante que nenhuma linha aprovada
    chega aqui sem chave.

    Normalizada com chave_comparacao: 'OS-2026-0001' e 'os-2026-0001 ' sao a
    mesma ordem apontada com caixa diferente, e precisam colidir de proposito.
    Sem isso, o mesmo apontamento salvo duas vezes com caixa diferente entraria
    como duas ordens e dobraria a producao do dia.
    """
    if not isinstance(data_entrada, date):
        raise ErroBanco(
            f"hash_linha: data_entrada precisa ser date, veio "
            f"{type(data_entrada).__name__} com valor {data_entrada!r}. "
            f"A limpeza deveria ter convertido antes"
        )

    partes = (
        chave_comparacao(numero_os),
        data_entrada.isoformat(),
        chave_comparacao(maquina),
    )
    return hashlib.sha256(SEPARADOR_CHAVE.join(partes).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Maquinas
# ---------------------------------------------------------------------------

def sincronizar_maquinas(
    conexao: sqlite3.Connection, cfg: Configuracao
) -> tuple[dict[str, int], list[str], list[str]]:
    """
    Garante uma linha em 'maquina' para cada nome canonico do config.

    Devolve o mapa nome -> id, os nomes criados agora e os que estao no banco mas
    o config nao conhece mais.

    Insere todas as maquinas do config, nao so as que apareceram no lote: uma
    maquina parada no mes e informacao, e sem a linha ela sumiria do painel em
    vez de aparecer com zero.

    A maquina que so existe no banco nao e apagada. Ela tem ordens historicas
    apontando para ela, e apagar quebraria a chave estrangeira - mas fica
    reportada, porque ordem antiga de maquina que o config nao declara mais nao
    tem meta com que ser comparada.
    """
    try:
        # A consulta vem antes do INSERT: depois dele toda maquina do config tem
        # linha, e nao haveria mais como saber quais acabaram de ser criadas.
        antes = {
            linha["nome_canonico"]
            for linha in conexao.execute("SELECT nome_canonico FROM maquina")
        }
        conexao.executemany(
            "INSERT INTO maquina (nome_canonico) VALUES (?) "
            "ON CONFLICT(nome_canonico) DO NOTHING",
            [(nome,) for nome in cfg.maquinas],
        )
        linhas = conexao.execute("SELECT id, nome_canonico FROM maquina").fetchall()
    except sqlite3.Error as erro:
        raise ErroBanco(f"nao foi possivel sincronizar as maquinas: {erro}") from erro

    ids = {linha["nome_canonico"]: linha["id"] for linha in linhas}
    criadas = sorted(nome for nome in cfg.maquinas if nome not in antes)
    fora_do_config = sorted(nome for nome in ids if nome not in cfg.maquinas)
    return ids, criadas, fora_do_config


# ---------------------------------------------------------------------------
# Gravacao das ordens
# ---------------------------------------------------------------------------

def _sql_gravar_ordem() -> str:
    """
    Monta o upsert a partir da lista de colunas, para nao repetir 18 nomes.

    A clausula WHERE e o que torna a reimportacao um no-op de verdade: sem ela,
    'importado_em' seria reescrito toda vez e o banco mudaria a cada execucao.
    'IS NOT' e a desigualdade do SQLite que trata NULL como valor - com '<>',
    uma coluna nula dos dois lados daria NULL em vez de falso e a comparacao
    inteira se perderia.
    """
    campos = ", ".join(COLUNAS_ORDEM)
    marcadores = ", ".join("?" * len(COLUNAS_ORDEM))
    atribuicoes = ", ".join(
        f"{coluna} = excluded.{coluna}"
        for coluna in COLUNAS_CONTEUDO + ("importado_em",)
    )
    comparacoes = " OR ".join(
        f"{coluna} IS NOT excluded.{coluna}" for coluna in COLUNAS_CONTEUDO
    )
    return (
        f"INSERT INTO ordem_producao ({campos}) VALUES ({marcadores}) "
        f"ON CONFLICT(hash_linha) DO UPDATE SET {atribuicoes} "
        f"WHERE {comparacoes}"
    )


def _data_em_texto(valor: Any) -> str | None:
    """Converte a data para o TEXT YYYY-MM-DD que o esquema guarda."""
    return None if valor is None else valor.isoformat()


def _valores_da_ordem(
    linha: dict[str, Any], ids_maquina: dict[str, int], momento: str
) -> tuple:
    """Monta a tupla de parametros de uma ordem, na ordem de COLUNAS_ORDEM."""
    maquina = linha["maquina"]
    if maquina not in ids_maquina:
        raise ErroBanco(
            f"maquina {maquina!r} da linha {linha[COLUNA_LINHA]} de "
            f"{linha[COLUNA_ARQUIVO]} nao tem id. "
            f"Conhecidas: {', '.join(sorted(ids_maquina))}"
        )

    return (
        hash_linha(linha["numero_os"], linha["data_entrada"], maquina),
        linha["numero_os"],
        linha["cliente"],
        linha["produto"],
        ids_maquina[maquina],
        linha["operador"],
        linha["turno"],
        _data_em_texto(linha["data_entrada"]),
        _data_em_texto(linha["data_prazo"]),
        _data_em_texto(linha["data_entrega"]),
        linha["status"],
        linha["tiragem_prevista"],
        linha["tiragem_boa"],
        linha["refugo"],
        linha["tempo_setup_min"],
        linha["tempo_producao_min"],
        linha[COLUNA_ARQUIVO],
        momento,
    )


def _colunas_divergentes(anterior: tuple, atual: tuple) -> list[str]:
    """Nomes das colunas de negocio em que duas linhas da mesma chave discordam."""
    posicoes = {coluna: COLUNAS_ORDEM.index(coluna) for coluna in COLUNAS_NEGOCIO}
    return [
        coluna
        for coluna, posicao in posicoes.items()
        if anterior[posicao] != atual[posicao]
    ]


def _separar_duplicadas(
    linhas: list[dict[str, Any]], ids_maquina: dict[str, int], momento: str
) -> tuple[list[tuple], list[Ocorrencia], int, int]:
    """
    Reduz o lote a uma linha por chave natural, registrando o que foi colapsado.

    A primeira linha de cada chave fica. 'Primeira' e na ordem em que a leitura
    entregou, que e alfabetica por arquivo e crescente por linha - a mesma pasta
    produz sempre a mesma escolha, que e o que a idempotencia exige.

    A segunda vira ocorrencia, com acao conforme o desfecho: copia exata e
    'duplicado', conteudo diferente e 'conflito'. Nos dois casos a mensagem
    aponta a linha da planilha que ficou no lugar; no conflito ela nomeia
    tambem as colunas que discordam.
    """
    parametros: list[tuple] = []
    vistos: dict[str, tuple[tuple, dict[str, Any]]] = {}
    ocorrencias: list[Ocorrencia] = []
    duplicadas = conflitos = 0

    for linha in linhas:
        valores = _valores_da_ordem(linha, ids_maquina, momento)
        chave = valores[0]

        anterior = vistos.get(chave)
        if anterior is None:
            vistos[chave] = (valores, linha)
            parametros.append(valores)
            continue

        valores_anteriores, linha_anterior = anterior
        divergentes = _colunas_divergentes(valores_anteriores, valores)
        if divergentes:
            conflitos += 1
            acao = ACAO_CONFLITO
            problema = PROBLEMA_CONFLITO.format(
                linha=linha_anterior[COLUNA_LINHA], colunas=", ".join(divergentes)
            )
        else:
            duplicadas += 1
            acao = ACAO_DUPLICADO
            problema = PROBLEMA_DUPLICADA.format(linha=linha_anterior[COLUNA_LINHA])

        ocorrencias.append(
            Ocorrencia(
                arquivo=linha[COLUNA_ARQUIVO],
                linha_planilha=linha[COLUNA_LINHA],
                coluna="numero_os",
                valor_original=str(linha["numero_os"]),
                problema=problema,
                acao=acao,
                valor_final="",
            )
        )

    return parametros, ocorrencias, duplicadas, conflitos


def _hashes_ja_gravados(
    conexao: sqlite3.Connection, hashes: list[str]
) -> set[str]:
    """Quais dos hashes do lote ja existem no banco."""
    encontrados: set[str] = set()
    for inicio in range(0, len(hashes), TAMANHO_DO_LOTE):
        lote = hashes[inicio : inicio + TAMANHO_DO_LOTE]
        marcadores = ", ".join("?" * len(lote))
        linhas = conexao.execute(
            f"SELECT hash_linha FROM ordem_producao WHERE hash_linha IN ({marcadores})",
            lote,
        ).fetchall()
        encontrados.update(linha["hash_linha"] for linha in linhas)
    return encontrados


def gravar_ordens(
    conexao: sqlite3.Connection,
    aprovadas: pd.DataFrame,
    cfg: Configuracao,
    momento: str,
) -> ResultadoGravacao:
    """
    Grava as ordens aprovadas, atualizando as que ja existem.

    'momento' e o carimbo de 'importado_em', recebido de fora para o teste poder
    fixa-lo e conferir que reimportar nao mexe na linha.
    """
    ids_maquina, criadas, fora_do_config = sincronizar_maquinas(conexao, cfg)

    resultado = ResultadoGravacao(
        maquinas_criadas=criadas, maquinas_fora_do_config=fora_do_config
    )
    if aprovadas.empty:
        return resultado

    parametros, ocorrencias, duplicadas, conflitos = _separar_duplicadas(
        aprovadas.to_dict("records"), ids_maquina, momento
    )
    resultado.ocorrencias = ocorrencias
    resultado.duplicadas = duplicadas
    resultado.conflitos = conflitos

    ja_gravados = _hashes_ja_gravados(conexao, [valores[0] for valores in parametros])
    resultado.inseridas = sum(
        1 for valores in parametros if valores[0] not in ja_gravados
    )

    antes = conexao.total_changes
    try:
        conexao.executemany(_sql_gravar_ordem(), parametros)
    except sqlite3.Error as erro:
        raise ErroBanco(f"nao foi possivel gravar as ordens: {erro}") from erro

    # total_changes conta linha inserida e linha atualizada. A linha que ja
    # estava identica nao conta, porque a clausula WHERE do upsert impediu a
    # escrita - e e exatamente essa a diferenca entre atualizada e inalterada.
    escritas = conexao.total_changes - antes
    resultado.atualizadas = escritas - resultado.inseridas
    resultado.inalteradas = len(parametros) - escritas

    if resultado.atualizadas < 0:
        raise ErroBanco(
            f"contagem inconsistente: {escritas} linha(s) escrita(s) para "
            f"{resultado.inseridas} insercao(oes) previstas. "
            f"O banco pode ter sido alterado por outro processo durante a importacao"
        )

    return resultado


def registrar_execucao(
    conexao: sqlite3.Connection,
    momento: str,
    arquivos_lidos: int,
    linhas_lidas: int,
    linhas_gravadas: int,
    linhas_corrigidas: int,
    linhas_rejeitadas: int,
    duracao_segundos: float,
) -> None:
    """
    Registra a execucao na tabela de historico.

    Esta tabela cresce a cada rodada de proposito, e e a unica coisa que muda
    quando se reimporta uma pasta inalterada: as ordens ficam identicas, o
    historico de importacoes ganha uma linha. E o que permite ver a entrada
    degradando ao longo do tempo, que e para o que serve a Decisao 2.
    """
    try:
        conexao.execute(
            "INSERT INTO execucao_importacao ("
            "executado_em, arquivos_lidos, linhas_lidas, linhas_gravadas, "
            "linhas_corrigidas, linhas_rejeitadas, duracao_segundos"
            ") VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                momento,
                arquivos_lidos,
                linhas_lidas,
                linhas_gravadas,
                linhas_corrigidas,
                linhas_rejeitadas,
                round(duracao_segundos, 3),
            ),
        )
    except sqlite3.Error as erro:
        raise ErroBanco(f"nao foi possivel registrar a execucao: {erro}") from erro


def contar_ordens(conexao: sqlite3.Connection) -> int:
    """Quantas ordens o banco tem. E o numero do criterio de aceite do bloco."""
    return conexao.execute("SELECT COUNT(*) AS total FROM ordem_producao").fetchone()[
        "total"
    ]


# ---------------------------------------------------------------------------
# Orquestracao
# ---------------------------------------------------------------------------

@dataclass
class ResultadoImportacao:
    """Uma importacao completa, da planilha ao banco."""

    arquivos_lidos: int
    validacao: ResultadoValidacao
    gravacao: ResultadoGravacao
    ocorrencias: list[Ocorrencia]
    ordens_no_banco: int
    duracao_segundos: float


def importar(
    cfg: Configuracao,
    entrada: Path | None = None,
    banco: Path | None = None,
    momento: str | None = None,
) -> ResultadoImportacao:
    """
    Le, limpa, valida e grava - a importacao inteira, num comando.

    A gravacao e o registro da execucao vao numa transacao so. Se algo falhar no
    meio, o banco volta ao estado anterior inteiro: meia importacao gravada seria
    pior que nenhuma, porque o proximo 'rodar de novo' partiria de um estado que
    ninguem sabe descrever.
    """
    inicio = time.perf_counter()
    momento = datetime.now().isoformat(timespec="seconds") if momento is None else momento

    leitura = ler_pasta(cfg, entrada)
    validacao = validar(leitura.dados, cfg)
    ocorrencias = list(validacao.ocorrencias)
    ocorrencias.extend(ocorrencias_de_descartes(leitura.descartes))

    conexao = conectar(cfg.caminho("banco") if banco is None else Path(banco))
    try:
        criar_esquema(conexao)
        with conexao:
            gravacao = gravar_ordens(conexao, validacao.aprovadas, cfg, momento)
            duracao = time.perf_counter() - inicio
            registrar_execucao(
                conexao,
                momento=momento,
                arquivos_lidos=len(leitura.arquivos),
                linhas_lidas=validacao.total_linhas,
                linhas_gravadas=gravacao.gravadas,
                linhas_corrigidas=validacao.linhas_corrigidas,
                linhas_rejeitadas=validacao.linhas_rejeitadas,
                duracao_segundos=duracao,
            )
        ocorrencias.extend(gravacao.ocorrencias)
        ordens = contar_ordens(conexao)
    finally:
        conexao.close()

    return ResultadoImportacao(
        arquivos_lidos=len(leitura.arquivos),
        validacao=validacao,
        gravacao=gravacao,
        ocorrencias=ocorrencias,
        ordens_no_banco=ordens,
        duracao_segundos=duracao,
    )


def descrever_importacao(resultado: ResultadoImportacao, cfg: Configuracao) -> str:
    """Monta o relatorio legivel da importacao."""
    gravacao = resultado.gravacao
    validacao = resultado.validacao

    linhas = [
        "Importacao",
        f"  arquivos lidos         {resultado.arquivos_lidos:>5}",
        f"  linhas de dado         {validacao.total_linhas:>5}",
        f"  aprovadas na validacao {len(validacao.aprovadas):>5}",
        f"  rejeitadas             {validacao.linhas_rejeitadas:>5}"
        f"  {validacao.taxa_rejeicao:>7.2%}",
        "",
        "Gravacao",
        f"  inseridas              {gravacao.inseridas:>5}",
        f"  atualizadas            {gravacao.atualizadas:>5}",
        f"  inalteradas            {gravacao.inalteradas:>5}",
        f"  duplicadas no lote     {gravacao.duplicadas:>5}",
        f"  conflitos de chave     {gravacao.conflitos:>5}",
        f"  {'-' * 30}",
        f"  ordens no banco        {resultado.ordens_no_banco:>5}",
        f"  duracao                {resultado.duracao_segundos:>8.2f} s",
    ]

    if gravacao.maquinas_fora_do_config:
        linhas.append("")
        linhas.append(
            "ATENCAO: maquinas no banco que o config.yaml nao declara mais. "
            "As ordens historicas delas ficam sem meta de produtividade:"
        )
        for nome in gravacao.maquinas_fora_do_config:
            linhas.append(f"  {nome}")

    conflitos = [o for o in gravacao.ocorrencias if o.acao == ACAO_CONFLITO]
    if conflitos:
        # Vem primeiro e separado: e o caso que exige alguem ir olhar a planilha.
        linhas.append("")
        linhas.append(f"CONFLITOS DE CHAVE NATURAL ({len(conflitos)}) - investigar")
        for ocorrencia in conflitos:
            linhas.append(
                f"  {ocorrencia.arquivo} linha {ocorrencia.linha_planilha} "
                f"({ocorrencia.valor_original}): {ocorrencia.problema}"
            )

    duplicadas = [o for o in gravacao.ocorrencias if o.acao == ACAO_DUPLICADO]
    if duplicadas:
        linhas.append("")
        linhas.append(f"Linhas duplicadas colapsadas ({len(duplicadas)})")
        for ocorrencia in duplicadas:
            linhas.append(
                f"  {ocorrencia.arquivo} linha {ocorrencia.linha_planilha} "
                f"({ocorrencia.valor_original}): {ocorrencia.problema}"
            )

    por_acao: dict[str, int] = {}
    for ocorrencia in resultado.ocorrencias:
        por_acao[ocorrencia.acao] = por_acao.get(ocorrencia.acao, 0) + 1
    linhas.append("")
    linhas.append(f"Relatorio de qualidade ({len(resultado.ocorrencias)} ocorrencias)")
    for acao in sorted(por_acao):
        linhas.append(f"  {acao:<22} {por_acao[acao]:>5}")

    return "\n".join(linhas)


def _argumentos(argv: list[str]) -> argparse.Namespace:
    analisador = argparse.ArgumentParser(
        prog="python -m src.banco",
        description="Importa as planilhas da pasta de entrada para o SQLite.",
    )
    analisador.add_argument(
        "--config", default=str(CONFIG_PADRAO), help="caminho do config.yaml"
    )
    analisador.add_argument("--entrada", help="sobrepoe caminhos.entrada")
    analisador.add_argument("--banco", help="sobrepoe caminhos.banco")
    return analisador.parse_args(argv)


def main(argv: list[str]) -> int:
    """
    Entrada de 'python -m src.banco'.

    Sai com 1 quando a taxa de rejeicao passa do limite, como na validacao - mas
    depois de gravar. As linhas aprovadas sao dado bom mesmo quando as rejeitadas
    passam do aceitavel; jogar fora as boas junto seria punir duas vezes. O
    codigo 1 avisa o agendador de que a entrada degradou.
    """
    args = _argumentos(argv)

    try:
        cfg = carregar(args.config)
    except ErroConfiguracao as erro:
        print(f"ERRO DE CONFIGURACAO: {erro}", file=sys.stderr)
        return CODIGO_SAIDA_ERRO_CONFIG

    try:
        resultado = importar(cfg, entrada=args.entrada, banco=args.banco)
    except ErroLeitura as erro:
        print(f"ERRO DE LEITURA: {erro}", file=sys.stderr)
        return 1
    except ErroValidacao as erro:
        print(f"ERRO DE VALIDACAO: {erro}", file=sys.stderr)
        return 1
    except ErroBanco as erro:
        print(f"ERRO DE BANCO: {erro}", file=sys.stderr)
        return 1

    print(descrever_importacao(resultado, cfg))

    if resultado.validacao.acima_do_limite(cfg):
        print(
            f"\nTaxa de rejeicao de {resultado.validacao.taxa_rejeicao:.2%} passa do "
            f"limite de {cfg.validacao['taxa_rejeicao_limite']:.0%} configurado em "
            f"'validacao.taxa_rejeicao_limite'",
            file=sys.stderr,
        )
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
