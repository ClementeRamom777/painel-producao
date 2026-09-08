"""
Calculo dos cinco indicadores da secao 5, a partir do banco.

O banco e a fonte da verdade daqui para frente. Nao se recalcula nada a partir
da planilha: o que esta em ordem_producao ja passou por limpeza, validacao e
deduplicacao, e e sobre esse conjunto que os numeros do painel precisam fechar.

Duas propriedades governam o modulo:

1. O indicador nunca inventa numero. Toda divisao passa por _divisao, que
   devolve None quando o denominador e zero - e None vira 'sem dado' na tela,
   nunca 'inf' e nunca 'NaN'. Ordem com tempo_producao_min igual a zero existe
   de verdade: e a ordem cancelada depois do acerto.

2. As somas ficam a vista. Cada Indicadores carrega os totais brutos que
   entraram na conta, e os cinco indicadores sao propriedades calculadas a
   partir deles. Assim e impossivel o numero exibido discordar das somas
   exibidas, e da para conferir na mao contra a mesma amostra.

O agrupamento mensal e por coorte de ENTRADA, nao por data de entrega: a ordem
conta no mes em que foi assumida. E o que mantem uma queda de OTD nitida no mes
em que ela aconteceu, em vez de borrada entre dois meses - a ordem que entra no
fim de abril e atrasa e entregue em maio.
"""

import argparse
import csv
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from src.banco import ErroBanco, conectar
from src.config import (
    CODIGO_SAIDA_ERRO_CONFIG,
    CONFIG_PADRAO,
    Configuracao,
    ErroConfiguracao,
    carregar,
)
from src.limpeza import STATUS_ENTREGUE
from src.validacao import CODIFICACAO_CSV, DELIMITADOR_CSV

# Rotulo do grupo quando a coluna de agrupamento esta vazia numa ordem aprovada.
#
# Turno e coluna opcional: celula vazia nela nao rejeita a linha, entao a ordem
# esta no banco sem turno. Sem este rotulo ela sumiria do corte por turno e a
# soma dos turnos deixaria de bater com o total, que e uma inconsistencia
# silenciosa na cara de quem le o painel.
#
# Isto NAO contradiz a secao 4.4. La o caso e valor ilegivel - a celula tinha
# conteudo e ele nao pode ser interpretado, e a linha inteira e rejeitada. Aqui a
# celula esta legitimamente vazia numa coluna que nao e obrigatoria, e a ordem e
# valida: o desconhecido precisa aparecer, porque ele existe.
SEM_VALOR = "(sem informacao)"

# Nome do grupo unico quando nao ha agrupamento.
GRUPO_TOTAL = "TOTAL"

# Expressao SQL de cada corte. O total usa um literal para a mesma consulta
# servir aos quatro casos sem um segundo caminho de codigo que possa divergir.
CORTES: dict[str, str] = {
    "total": f"'{GRUPO_TOTAL}'",
    "maquina": "maquina.nome_canonico",
    "turno": f"COALESCE(ordem_producao.turno, '{SEM_VALOR}')",
    "mes": "substr(ordem_producao.data_entrada, 1, 7)",
    # Cliente nao entra em ResultadoIndicadores: a secao 5 pede os indicadores
    # no total, por maquina, por turno e por mes, e so o relatorio Excel corta
    # por cliente. Fica disponivel aqui, por calcular_corte, para o Bloco 9 nao
    # ter que escrever uma segunda consulta de agregacao que possa divergir
    # desta - mas sem o painel pagar por uma consulta que nao usa.
    "cliente": f"COALESCE(ordem_producao.cliente, '{SEM_VALOR}')",
}

# Sentido da comparacao com a meta: 'maior' e quanto maior melhor.
# Produtividade nao esta aqui porque a meta dela e por maquina, nao global -
# folha boa por hora nao e comparavel entre equipamentos de portes diferentes.
SENTIDO_DA_META: dict[str, str] = {
    "refugo_pct": "menor",
    "setup_medio_min": "menor",
    "otd_pct": "maior",
    "aproveitamento_pct": "maior",
    # Nao e atributo de Indicadores: sai de maquinas_na_meta, que cruza o corte
    # por maquina com 'metas_produtividade'. Esta aqui porque e o indicador
    # global que substitui a produtividade no cartao, e precisa da mesma
    # comparacao com meta que os outros quatro.
    "maquinas_na_meta_pct": "maior",
}

# Chave de 'metas' no config.yaml que cada indicador compara.
CHAVE_DA_META: dict[str, str] = {
    "refugo_pct": "refugo_maximo_pct",
    "setup_medio_min": "setup_maximo_min",
    "otd_pct": "otd_minimo_pct",
    "aproveitamento_pct": "aproveitamento_minimo_pct",
    "maquinas_na_meta_pct": "maquinas_na_meta_minimo_pct",
}


class ErroIndicadores(Exception):
    """Periodo invalido ou banco sem os dados necessarios."""


def _divisao(numerador: float, denominador: float) -> float | None:
    """
    Divide, devolvendo None quando nao ha denominador.

    E a guarda obrigatoria da secao 5. Ordem com tempo de producao zero existe na
    vida real - a cancelada depois do acerto - e sem esta funcao o painel
    mostraria 'inf' ou 'NaN' numa tela que a gerencia le como se fosse verdade.
    """
    if not denominador:
        return None
    return numerador / denominador


@dataclass(frozen=True)
class Indicadores:
    """
    Os cinco indicadores de um corte, com as somas que os produziram.

    As somas sao os campos; os indicadores sao propriedades derivadas delas. E
    de proposito: guardar os dois como campos permitiria que um fosse atualizado
    sem o outro, e o painel exibiria um numero que as somas ao lado desmentem.
    """

    rotulo: str
    ordens: int
    tiragem_boa: int
    refugo: int
    tempo_producao_min: float
    tempo_setup_min: float
    ordens_com_setup: int
    entregues_com_prazo: int
    entregues_no_prazo: int
    entregues_sem_prazo: int

    @property
    def produtividade(self) -> float | None:
        """Folhas boas por hora de maquina."""
        return _divisao(self.tiragem_boa, self.tempo_producao_min / 60)

    @property
    def refugo_pct(self) -> float | None:
        """Percentual de material perdido sobre o que passou pela maquina."""
        return _divisao(self.refugo * 100, self.tiragem_boa + self.refugo)

    @property
    def setup_medio_min(self) -> float | None:
        """
        Minutos parados por job.

        E media simples por ordem, e nao ponderada como os outros: a especificacao
        pede 'media(tempo_setup_min)', e o que a gerencia cobra e o acerto tipico
        de um job, nao o acerto por folha produzida.
        """
        return _divisao(self.tempo_setup_min, self.ordens_com_setup)

    @property
    def otd_pct(self) -> float | None:
        """Percentual de entrega no prazo, sobre as ordens que tem prazo."""
        return _divisao(self.entregues_no_prazo * 100, self.entregues_com_prazo)

    @property
    def aproveitamento_pct(self) -> float | None:
        """Quanto do tempo de maquina foi producao, e nao acerto."""
        return _divisao(
            self.tempo_producao_min * 100,
            self.tempo_producao_min + self.tempo_setup_min,
        )


@dataclass
class ResultadoIndicadores:
    """Os indicadores no total e nos tres cortes, com o periodo analisado."""

    total: Indicadores
    por_maquina: dict[str, Indicadores] = field(default_factory=dict)
    por_turno: dict[str, Indicadores] = field(default_factory=dict)
    por_mes: dict[str, Indicadores] = field(default_factory=dict)
    de: str = ""
    ate: str = ""
    mes_incompleto: str | None = None


# ---------------------------------------------------------------------------
# Consulta
# ---------------------------------------------------------------------------

def _sql_do_corte(corte: str) -> str:
    """
    Monta a consulta de agregacao de um corte.

    'entregue' e repetido em tres CASE porque as tres contagens do OTD partem do
    mesmo conjunto: a ordem entregue de fato, com data de entrega preenchida. A
    ordem com status ENTREGUE e sem data ja foi rejeitada na validacao, mas a
    condicao fica aqui tambem - esta consulta e a ultima linha de defesa antes
    do numero ir para a tela.
    """
    grupo = CORTES[corte]
    entregue = (
        "ordem_producao.status = :status_entregue "
        "AND ordem_producao.data_entrega IS NOT NULL"
    )
    return f"""
        SELECT
            {grupo} AS grupo,
            COUNT(*) AS ordens,
            COALESCE(SUM(ordem_producao.tiragem_boa), 0) AS tiragem_boa,
            COALESCE(SUM(ordem_producao.refugo), 0) AS refugo,
            COALESCE(SUM(ordem_producao.tempo_producao_min), 0.0) AS tempo_producao_min,
            COALESCE(SUM(ordem_producao.tempo_setup_min), 0.0) AS tempo_setup_min,
            COUNT(ordem_producao.tempo_setup_min) AS ordens_com_setup,
            SUM(CASE WHEN {entregue}
                      AND ordem_producao.data_prazo IS NOT NULL
                     THEN 1 ELSE 0 END) AS entregues_com_prazo,
            SUM(CASE WHEN {entregue}
                      AND ordem_producao.data_prazo IS NOT NULL
                      AND ordem_producao.data_entrega <= ordem_producao.data_prazo
                     THEN 1 ELSE 0 END) AS entregues_no_prazo,
            SUM(CASE WHEN {entregue}
                      AND ordem_producao.data_prazo IS NULL
                     THEN 1 ELSE 0 END) AS entregues_sem_prazo
        FROM ordem_producao
        JOIN maquina ON maquina.id = ordem_producao.maquina_id
        WHERE ordem_producao.data_entrada BETWEEN :de AND :ate
        GROUP BY grupo
        ORDER BY grupo
    """


def _indicadores_da_linha(linha: sqlite3.Row) -> Indicadores:
    """Converte uma linha agregada da consulta em Indicadores."""
    return Indicadores(
        rotulo=linha["grupo"],
        ordens=linha["ordens"],
        tiragem_boa=linha["tiragem_boa"],
        refugo=linha["refugo"],
        tempo_producao_min=linha["tempo_producao_min"],
        tempo_setup_min=linha["tempo_setup_min"],
        ordens_com_setup=linha["ordens_com_setup"],
        entregues_com_prazo=linha["entregues_com_prazo"],
        entregues_no_prazo=linha["entregues_no_prazo"],
        entregues_sem_prazo=linha["entregues_sem_prazo"],
    )


def _vazio(rotulo: str) -> Indicadores:
    """Um corte sem nenhuma ordem. Todos os indicadores dele saem None."""
    return Indicadores(
        rotulo=rotulo,
        ordens=0,
        tiragem_boa=0,
        refugo=0,
        tempo_producao_min=0.0,
        tempo_setup_min=0.0,
        ordens_com_setup=0,
        entregues_com_prazo=0,
        entregues_no_prazo=0,
        entregues_sem_prazo=0,
    )


def calcular_corte(
    conexao: sqlite3.Connection, corte: str, de: str, ate: str
) -> dict[str, Indicadores]:
    """Roda a agregacao de um corte e devolve rotulo -> Indicadores."""
    parametros = {"de": de, "ate": ate, "status_entregue": STATUS_ENTREGUE}
    try:
        linhas = conexao.execute(_sql_do_corte(corte), parametros).fetchall()
    except sqlite3.Error as erro:
        raise ErroIndicadores(
            f"nao foi possivel calcular o corte {corte!r}: {erro}"
        ) from erro
    return {linha["grupo"]: _indicadores_da_linha(linha) for linha in linhas}


def _limites_do_banco(conexao: sqlite3.Connection) -> tuple[str | None, str | None]:
    """Primeira e ultima data de entrada gravadas, em texto ISO."""
    linha = conexao.execute(
        "SELECT MIN(data_entrada) AS inicio, MAX(data_entrada) AS fim "
        "FROM ordem_producao"
    ).fetchone()
    return linha["inicio"], linha["fim"]


def calcular(
    conexao: sqlite3.Connection,
    de: str | None = None,
    ate: str | None = None,
) -> ResultadoIndicadores:
    """
    Calcula os indicadores no total e por maquina, turno e mes.

    O periodo filtra pela data de ENTRADA, que e a coorte do agrupamento mensal.
    Omitido, usa o intervalo inteiro que esta no banco.
    """
    inicio, fim = _limites_do_banco(conexao)
    if inicio is None:
        raise ErroIndicadores(
            "o banco nao tem nenhuma ordem. Rode 'python -m src.banco' antes"
        )

    de = inicio if de is None else de
    ate = fim if ate is None else ate
    if de > ate:
        raise ErroIndicadores(
            f"periodo invertido: --de {de} e posterior a --ate {ate}"
        )

    total = calcular_corte(conexao, "total", de, ate)
    resultado = ResultadoIndicadores(
        total=total.get(GRUPO_TOTAL, _vazio(GRUPO_TOTAL)),
        por_maquina=calcular_corte(conexao, "maquina", de, ate),
        por_turno=calcular_corte(conexao, "turno", de, ate),
        por_mes=calcular_corte(conexao, "mes", de, ate),
        de=de,
        ate=ate,
    )

    # O ultimo mes com ordens no BANCO, nao no periodo filtrado. A censura a
    # direita da secao 4.4 vem do corte da exportacao, que e propriedade do dado:
    # a ordem que entrou perto do fim ainda nao teve tempo de ser entregue, e
    # como o atraso empurra a entrega para frente, o corte remove seletivamente
    # as ordens ruins e o OTD do mes sai otimista. Filtrar um periodo mais curto
    # nao cria censura nenhuma, entao o mes marcado tem que sair do banco todo.
    mes_final = fim[:7]
    if mes_final in resultado.por_mes:
        resultado.mes_incompleto = mes_final

    return resultado


# ---------------------------------------------------------------------------
# Comparacao com as metas
# ---------------------------------------------------------------------------

def meta_de(indicador: str, cfg: Configuracao) -> float:
    """Valor da meta configurada para um indicador de meta global."""
    if indicador not in CHAVE_DA_META:
        raise ErroIndicadores(
            f"indicador {indicador!r} nao tem meta global. "
            f"Com meta global: {', '.join(sorted(CHAVE_DA_META))}. "
            f"Produtividade tem meta por maquina, em 'metas_produtividade'"
        )
    return cfg.metas[CHAVE_DA_META[indicador]]


def na_meta(indicador: str, valor: float | None, meta: float) -> bool | None:
    """
    Diz se o valor cumpre a meta. None quando nao ha valor para comparar.

    O None se propaga de proposito: 'sem dado' nao e nem verde nem vermelho, e
    pintar de verde o que nao foi medido e a forma mais facil de um painel
    mentir.
    """
    if valor is None:
        return None
    if SENTIDO_DA_META[indicador] == "maior":
        return valor >= meta
    return valor <= meta


def produtividade_na_meta(
    maquina: str, indicadores: Indicadores, cfg: Configuracao
) -> bool | None:
    """Compara a produtividade de uma maquina com a meta dela."""
    if maquina not in cfg.metas_produtividade:
        return None
    if indicadores.produtividade is None:
        return None
    return indicadores.produtividade >= cfg.metas_produtividade[maquina]


def maquinas_na_meta(
    resultado: ResultadoIndicadores, cfg: Configuracao
) -> tuple[int, int, float | None]:
    """
    Quantas maquinas batem a propria meta de produtividade, de quantas medidas.

    O cartao global de produtividade mostra isto, e nao uma media de folhas por
    hora: media entre uma plana e uma digital nao tem meta que signifique nada. A
    maquina sem produtividade medida no periodo nao entra em nenhum dos dois
    lados - ela nao falhou a meta, ela nao foi medida.
    """
    avaliadas = [
        produtividade_na_meta(maquina, indicadores, cfg)
        for maquina, indicadores in resultado.por_maquina.items()
    ]
    medidas = [resposta for resposta in avaliadas if resposta is not None]
    if not medidas:
        return 0, 0, None
    dentro = sum(medidas)
    return dentro, len(medidas), dentro / len(medidas) * 100


# ---------------------------------------------------------------------------
# Apresentacao no terminal
# ---------------------------------------------------------------------------

def _numero(valor: float | None, casas: int = 1, sufixo: str = "") -> str:
    """Formata um indicador, ou 'sem dado' quando ele nao pode ser calculado."""
    if valor is None:
        return "sem dado"
    return f"{valor:,.{casas}f}{sufixo}".replace(",", " ")


def _marca(resposta: bool | None) -> str:
    """Traduz o resultado da comparacao com a meta."""
    if resposta is None:
        return ""
    return "na meta" if resposta else "FORA"


def _linha_do_corte(rotulo: str, indicadores: Indicadores) -> str:
    """Uma linha da tabela de um corte, com os cinco indicadores."""
    return (
        f"  {rotulo:<22} {indicadores.ordens:>5} "
        f"{_numero(indicadores.produtividade, 0):>12} "
        f"{_numero(indicadores.refugo_pct, 2):>9} "
        f"{_numero(indicadores.setup_medio_min, 1):>9} "
        f"{_numero(indicadores.otd_pct, 1):>9} "
        f"{_numero(indicadores.aproveitamento_pct, 1):>9}"
    )


def _cabecalho_do_corte(titulo: str) -> list[str]:
    return [
        "",
        titulo,
        f"  {'':<22} {'ordens':>5} {'folhas/h':>12} {'refugo%':>9} "
        f"{'setup':>9} {'OTD%':>9} {'aprov%':>9}",
    ]


def descrever(resultado: ResultadoIndicadores, cfg: Configuracao) -> str:
    """Monta o relatorio legivel dos indicadores."""
    total = resultado.total
    dentro, medidas, pct_maquinas = maquinas_na_meta(resultado, cfg)

    linhas = [
        f"Periodo por coorte de entrada: {resultado.de} a {resultado.ate}",
        f"Ordens no periodo: {total.ordens}",
        "",
        "Indicadores no total",
    ]

    # Produtividade global e 'X de N maquinas na meta', nao uma media de
    # folhas/hora: a media entre plana e digital nao tem meta que signifique nada.
    meta_maquinas = cfg.metas["maquinas_na_meta_minimo_pct"]
    if pct_maquinas is None:
        linhas.append(
            f"  {'produtividade':<18} sem maquina medida no periodo"
        )
    else:
        estado = "na meta" if pct_maquinas >= meta_maquinas else "FORA"
        linhas.append(
            f"  {'produtividade':<18} {dentro} de {medidas} maquinas na meta "
            f"({_numero(pct_maquinas, 0, '%')})   minimo {meta_maquinas:.0f}%   {estado}"
        )

    for indicador, casas, sufixo in (
        ("refugo_pct", 2, "%"),
        ("setup_medio_min", 1, " min"),
        ("otd_pct", 1, "%"),
        ("aproveitamento_pct", 1, "%"),
    ):
        valor = getattr(total, indicador)
        meta = meta_de(indicador, cfg)
        sentido = "maximo" if SENTIDO_DA_META[indicador] == "menor" else "minimo"
        linhas.append(
            f"  {indicador:<18} {_numero(valor, casas, sufixo):>12}   "
            f"{sentido} {meta:g}   {_marca(na_meta(indicador, valor, meta))}"
        )

    if total.entregues_sem_prazo:
        linhas.append("")
        linhas.append(
            f"  ATENCAO: {total.entregues_sem_prazo} ordem(ns) entregue(s) sem data de "
            f"prazo ficaram fora do OTD. Sem prazo nao ha como dizer se atrasou."
        )

    linhas += _cabecalho_do_corte("Por maquina (meta de folhas/h e propria de cada uma)")
    for maquina, indicadores in resultado.por_maquina.items():
        marca = _marca(produtividade_na_meta(maquina, indicadores, cfg))
        meta = cfg.metas_produtividade.get(maquina)
        alvo = f"meta {meta:,.0f}".replace(",", " ") if meta else "sem meta"
        linhas.append(
            f"{_linha_do_corte(maquina, indicadores)}   {alvo:>12} {marca}"
        )

    linhas += _cabecalho_do_corte("Por turno")
    for turno, indicadores in resultado.por_turno.items():
        linhas.append(_linha_do_corte(f"turno {turno}", indicadores))

    linhas += _cabecalho_do_corte("Por mes de entrada")
    for mes, indicadores in resultado.por_mes.items():
        marca = "  <- periodo incompleto" if mes == resultado.mes_incompleto else ""
        linhas.append(_linha_do_corte(mes, indicadores) + marca)

    if resultado.mes_incompleto:
        linhas.append("")
        linhas.append(
            f"  {resultado.mes_incompleto} e o ultimo mes do banco e sofre censura a "
            f"direita: a ordem que entrou perto do corte da exportacao ainda nao foi"
        )
        linhas.append(
            "  entregue e nao entra no OTD. Como o atraso empurra a entrega para "
            "frente, o corte remove mais ordem ruim que boa e o OTD do mes sai"
        )
        linhas.append(
            "  otimista. O painel do Bloco 8 precisa marcar este ponto, nunca "
            "esconder."
        )

    return "\n".join(linhas)


def descrever_conferencia(
    maquina: str, resultado: ResultadoIndicadores, cfg: Configuracao
) -> str:
    """
    Monta o bloco de conferencia manual de uma maquina.

    Mostra as somas que entraram em cada conta e a conta armada com elas, para
    o numero poder ser refeito no Excel sobre a mesma amostra. Sem os
    intermediarios a vista, conferir um indicador exige confiar nele.
    """
    if maquina not in resultado.por_maquina:
        conhecidas = ", ".join(resultado.por_maquina) or "(nenhuma no periodo)"
        raise ErroIndicadores(
            f"maquina {maquina!r} nao tem ordem no periodo "
            f"{resultado.de} a {resultado.ate}. Com ordens: {conhecidas}"
        )

    ind = resultado.por_maquina[maquina]
    meta_produtividade = cfg.metas_produtividade.get(maquina)

    linhas = [
        f"Conferencia manual: {maquina}",
        f"  periodo por coorte de entrada: {resultado.de} a {resultado.ate}",
        f"  ordens no corte: {ind.ordens}",
        "",
        "  Somas (e o que conferir com SOMA() no Excel)",
        f"    SOMA(tiragem_boa)          {ind.tiragem_boa}",
        f"    SOMA(refugo)               {ind.refugo}",
        f"    SOMA(tempo_producao_min)   {ind.tempo_producao_min:.1f}",
        f"    SOMA(tempo_setup_min)      {ind.tempo_setup_min:.1f}",
        f"    ordens com setup           {ind.ordens_com_setup}",
        f"    entregues com prazo        {ind.entregues_com_prazo}",
        f"    entregues no prazo         {ind.entregues_no_prazo}",
        f"    entregues sem prazo        {ind.entregues_sem_prazo}",
        "",
        "  Contas",
    ]

    def conta(nome: str, formula: str, valor: float | None, casas: int, alvo: str) -> str:
        return f"    {nome:<16} {formula:<44} = {_numero(valor, casas):>10}   {alvo}"

    if meta_produtividade is None:
        alvo_produtividade = "sem meta configurada"
    else:
        marca = _marca(produtividade_na_meta(maquina, ind, cfg))
        alvo_produtividade = f"meta {meta_produtividade:g}   {marca}"

    linhas.append(
        conta(
            "produtividade",
            f"{ind.tiragem_boa} / ({ind.tempo_producao_min:.1f} / 60)",
            ind.produtividade,
            1,
            alvo_produtividade,
        )
    )

    for nome, formula, indicador, casas in (
        (
            "refugo %",
            f"{ind.refugo} / ({ind.tiragem_boa} + {ind.refugo}) * 100",
            "refugo_pct",
            2,
        ),
        (
            "setup medio",
            f"{ind.tempo_setup_min:.1f} / {ind.ordens_com_setup}",
            "setup_medio_min",
            1,
        ),
        (
            "OTD %",
            f"{ind.entregues_no_prazo} / {ind.entregues_com_prazo} * 100",
            "otd_pct",
            1,
        ),
        (
            "aproveitamento %",
            f"{ind.tempo_producao_min:.1f} / "
            f"({ind.tempo_producao_min:.1f} + {ind.tempo_setup_min:.1f}) * 100",
            "aproveitamento_pct",
            1,
        ),
    ):
        valor = getattr(ind, indicador)
        meta = meta_de(indicador, cfg)
        sentido = "maximo" if SENTIDO_DA_META[indicador] == "menor" else "minimo"
        alvo = f"{sentido} {meta:g}   {_marca(na_meta(indicador, valor, meta))}"
        linhas.append(conta(nome, formula, valor, casas, alvo))

    return "\n".join(linhas)


def exportar_conferencia(
    conexao: sqlite3.Connection,
    maquina: str,
    resultado: ResultadoIndicadores,
    destino: Path,
) -> int:
    """
    Grava em CSV as ordens que entraram nas somas de uma maquina.

    Sem isso a conferencia manual nao fecha: a planilha de entrada tem as linhas
    rejeitadas e as duplicadas, que o banco nao tem. Este CSV e exatamente a
    amostra somada, entao o SOMA() do Excel sobre ele tem que bater com o
    numero impresso, coluna por coluna.
    """
    colunas = (
        "numero_os", "cliente", "produto", "operador", "turno",
        "data_entrada", "data_prazo", "data_entrega", "status",
        "tiragem_prevista", "tiragem_boa", "refugo",
        "tempo_setup_min", "tempo_producao_min", "arquivo_origem",
    )
    selecao = ", ".join(f"ordem_producao.{coluna}" for coluna in colunas)
    linhas = conexao.execute(
        f"SELECT {selecao} FROM ordem_producao "
        f"JOIN maquina ON maquina.id = ordem_producao.maquina_id "
        f"WHERE maquina.nome_canonico = :maquina "
        f"AND ordem_producao.data_entrada BETWEEN :de AND :ate "
        f"ORDER BY ordem_producao.data_entrada, ordem_producao.numero_os",
        {"maquina": maquina, "de": resultado.de, "ate": resultado.ate},
    ).fetchall()

    destino.parent.mkdir(parents=True, exist_ok=True)
    # Mesmo delimitador e mesma codificacao do relatorio de qualidade: os dois
    # CSV do projeto precisam abrir do mesmo jeito no Excel. A decisao mora em
    # validacao.py, junto de Ocorrencia.
    with destino.open("w", encoding=CODIFICACAO_CSV, newline="") as arquivo:
        escritor = csv.writer(arquivo, delimiter=DELIMITADOR_CSV)
        escritor.writerow(colunas)
        for linha in linhas:
            escritor.writerow([linha[coluna] for coluna in colunas])

    return len(linhas)


# ---------------------------------------------------------------------------
# Linha de comando
# ---------------------------------------------------------------------------

def _data_valida(texto: str) -> str:
    """Confere que o texto e uma data ISO, e devolve como veio."""
    try:
        date.fromisoformat(texto)
    except ValueError as erro:
        raise ErroIndicadores(
            f"{texto!r} nao e uma data no formato AAAA-MM-DD ({erro})"
        ) from erro
    return texto


def _argumentos(argv: list[str]) -> argparse.Namespace:
    analisador = argparse.ArgumentParser(
        prog="python -m src.indicadores",
        description="Calcula os indicadores de producao a partir do banco.",
    )
    analisador.add_argument(
        "--config", default=str(CONFIG_PADRAO), help="caminho do config.yaml"
    )
    analisador.add_argument("--banco", help="sobrepoe caminhos.banco")
    analisador.add_argument("--de", help="inicio do periodo, AAAA-MM-DD")
    analisador.add_argument("--ate", help="fim do periodo, AAAA-MM-DD")
    analisador.add_argument(
        "--maquina", help="imprime a conferencia manual desta maquina"
    )
    analisador.add_argument(
        "--csv", help="grava as ordens da maquina de --maquina neste arquivo"
    )
    return analisador.parse_args(argv)


def main(argv: list[str]) -> int:
    """Entrada de 'python -m src.indicadores'."""
    args = _argumentos(argv)

    try:
        cfg = carregar(args.config)
    except ErroConfiguracao as erro:
        print(f"ERRO DE CONFIGURACAO: {erro}", file=sys.stderr)
        return CODIGO_SAIDA_ERRO_CONFIG

    if args.csv and not args.maquina:
        print(
            "ERRO: --csv exporta a amostra de uma maquina e precisa de --maquina",
            file=sys.stderr,
        )
        return 1

    caminho = cfg.caminho("banco") if args.banco is None else Path(args.banco)
    if not caminho.is_file():
        print(
            f"ERRO: banco nao encontrado em {caminho}. "
            f"Rode 'python -m src.banco' antes",
            file=sys.stderr,
        )
        return 1

    try:
        conexao = conectar(caminho)
    except ErroBanco as erro:
        print(f"ERRO DE BANCO: {erro}", file=sys.stderr)
        return 1

    try:
        de = _data_valida(args.de) if args.de else None
        ate = _data_valida(args.ate) if args.ate else None
        resultado = calcular(conexao, de, ate)

        print(descrever(resultado, cfg))

        if args.maquina:
            print("")
            print(descrever_conferencia(args.maquina, resultado, cfg))

            if args.csv:
                destino = Path(args.csv)
                gravadas = exportar_conferencia(
                    conexao, args.maquina, resultado, destino
                )
                print("")
                print(f"  {gravadas} ordens gravadas em {destino}")
                print("  Some as colunas nele: os totais tem que bater com os de cima.")
    except ErroIndicadores as erro:
        print(f"ERRO DE INDICADORES: {erro}", file=sys.stderr)
        return 1
    finally:
        conexao.close()

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
