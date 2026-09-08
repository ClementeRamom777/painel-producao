"""
Validacao: separa o que e aproveitavel do que e inconsistente.

Este modulo faz duas coisas, nessa ordem:

1. Passa cada celula crua pelas funcoes do Bloco 4 e guarda o que foi corrigido.
2. Aplica as regras de negocio sobre os valores ja limpos e separa o DataFrame
   em aprovadas e rejeitadas.

O passo 1 esta aqui, e nao na limpeza, por uma escolha que a especificacao nao
resolve: o Bloco 4 pede funcoes que nao conhecem pandas nem planilha, e alguem
precisa aplica-las linha a linha. A validacao e o primeiro modulo que depende
dos valores limpos, e e ela que monta o relatorio de qualidade - onde correcao e
rejeicao aparecem lado a lado, com o mesmo formato de linha. Separar em um
sexto modulo so para aplicar cinco funcoes criaria uma camada sem regra propria.

Decisao 2 da especificacao, e a razao de existir do modulo: nenhuma linha some
sem deixar rastro. Toda celula corrigida e toda linha rejeitada vira uma
Ocorrencia com arquivo, linha da planilha, coluna, valor original, problema,
acao e valor final - exatamente as colunas de 'saida/relatorio_qualidade.csv'
da secao 6.3.

A linha rejeitada nao e apagada: ela sai num DataFrame proprio, com os motivos.
Quem grava no banco usa so as aprovadas; quem monta o relatorio usa as duas.

Uma inconsistencia da secao 4.2 nao e tratada aqui de proposito: a LINHA
DUPLICADA. Ela e copia exata de outra, entao passa por todas as regras deste
modulo - e tem que passar, porque as duas linhas sao dado valido. Deduplicar e
do Bloco 6, pelo hash da chave natural (Decisao 3), e e la que ela precisa
deixar rastro: o ON CONFLICT DO UPDATE colapsa as duas em silencio se ninguem
contar quantas colidiram.
"""

import argparse
import csv
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field, fields
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
from src.leitura import (
    COLUNA_ARQUIVO,
    COLUNA_LINHA,
    Descarte,
    ErroLeitura,
    descrever_resultado as descrever_leitura,
    ler_pasta,
)
from src.limpeza import (
    STATUS_CANCELADA,
    STATUS_ENTREGUE,
    TIPO_NUMERICO_POR_COLUNA,
    ValorLimpo,
    converter_data,
    converter_numero,
    normalizar_maquina,
    normalizar_status,
    normalizar_texto,
    normalizar_turno,
)

# Coluna acrescentada ao DataFrame de rejeitadas com os motivos da linha.
COLUNA_MOTIVOS = "motivos"

# As acoes que uma ocorrencia pode registrar. A secao 6.3 declara duas -
# corrigido e rejeitado. As outras duas sao para linhas que nao sao rejeicao de
# qualidade e por isso ficam fora da taxa, mas que sumiriam em silencio se nao
# aparecessem no relatorio:
#
# - 'descartado': o que a leitura jogou fora antes de chegar aqui, linha em
#   branco e linha TOTAL de rodape. Nunca foram ordem de producao.
# - 'duplicado': a linha que o Bloco 6 nao gravou porque outra, identica, ja
#   ocupava a mesma chave natural. E benigna - dado igual, colapso sem perda -
#   mas o ON CONFLICT DO UPDATE a apagaria sem deixar rastro nenhum.
# - 'conflito': a linha que ocupava a mesma chave natural de outra e DISCORDAVA
#   dela. Nao e repeticao, sao duas versoes da mesma ordem, e o sistema escolheu
#   uma sozinho. Tem acao propria e nao entra em 'duplicado' de proposito:
#   juntar as duas faria o filtro do CSV misturar as dezesseis linhas
#   inofensivas com o caso raro que exige alguem ir olhar a planilha.
ACAO_CORRIGIDO = "corrigido"
ACAO_REJEITADO = "rejeitado"
ACAO_DESCARTADO = "descartado"
ACAO_DUPLICADO = "duplicado"
ACAO_CONFLITO = "conflito"

MOTIVO_ILEGIVEL = "valor ilegivel"
MOTIVO_OBRIGATORIO_VAZIO = "campo obrigatorio vazio"
MOTIVO_TIRAGEM_BOA_VAZIA = "tiragem boa vazia em ordem nao cancelada"
MOTIVO_VALOR_NEGATIVO = "valor negativo"
MOTIVO_TEMPO_ACIMA_DO_MAXIMO = "tempo acima do maximo"
MOTIVO_TEMPO_TOTAL_ACIMA_DO_MAXIMO = "tempo total acima do maximo"
MOTIVO_REFUGO_ACIMA_DO_LIMITE = "refugo acima do limite"
MOTIVO_PRAZO_ANTES_DA_ENTRADA = "prazo anterior a entrada"
MOTIVO_ENTREGA_ANTES_DA_ENTRADA = "entrega anterior a entrada"
MOTIVO_STATUS_INCOERENTE = "status incoerente com a entrega"

# Status que as regras deste modulo consultam pelo significado. Conferidos contra
# o config antes de validar, para o config nao poder renomear um deles e deixar a
# regra sem efeito em silencio.
STATUS_EXIGIDOS = (STATUS_ENTREGUE, STATUS_CANCELADA)


class ErroValidacao(Exception):
    """Configuracao incompativel com o que a validacao precisa para rodar."""


@dataclass(frozen=True)
class Ocorrencia:
    """
    Uma linha do relatorio de qualidade.

    Os campos sao os da secao 6.3, na mesma ordem, para o CSV do Bloco 10 sair
    daqui sem tradutor no meio.
    """

    arquivo: str
    linha_planilha: int
    coluna: str
    valor_original: str
    problema: str
    acao: str
    valor_final: str


@dataclass
class ResultadoValidacao:
    """O que a validacao produziu, e tudo que ela precisou decidir no caminho."""

    aprovadas: pd.DataFrame
    rejeitadas: pd.DataFrame
    ocorrencias: list[Ocorrencia] = field(default_factory=list)
    total_linhas: int = 0
    linhas_por_motivo: dict[str, int] = field(default_factory=dict)
    linhas_corrigidas: int = 0

    @property
    def linhas_rejeitadas(self) -> int:
        return len(self.rejeitadas)

    @property
    def taxa_rejeicao(self) -> float:
        """
        Fracao das linhas de dado que foram rejeitadas.

        O denominador e a linha de dado lida, nao a ordem gravada: o descarte de
        rodape da leitura fica de fora dos dois lados da conta. Sem linha
        nenhuma a taxa e zero - pasta vazia nao e degradacao de qualidade, e a
        leitura ja recusa antes de chegar aqui.
        """
        if self.total_linhas == 0:
            return 0.0
        return self.linhas_rejeitadas / self.total_linhas

    def acima_do_limite(self, cfg: Configuracao) -> bool:
        """Diz se a taxa passou de 'validacao.taxa_rejeicao_limite' (codigo 1)."""
        return self.taxa_rejeicao > cfg.validacao["taxa_rejeicao_limite"]


def _texto(valor: Any) -> str:
    """Representacao do valor para a celula do relatorio. Vazio vira vazio."""
    return "" if valor is None else str(valor)


# Como o CSV de saida e escrito. Ponto e virgula e o separador de lista do
# Windows em pt-BR, e utf-8-sig poe o BOM que faz o Excel reconhecer UTF-8 - sem
# ele o Excel le como latin-1 e estraga todo acento. Declarado aqui, junto de
# Ocorrencia, porque o relatorio de qualidade e o CSV de conferencia dos
# indicadores precisam abrir do mesmo jeito, e duas copias da decisao sairiam de
# sincronia na primeira troca.
DELIMITADOR_CSV = ";"
CODIFICACAO_CSV = "utf-8-sig"

# As colunas da secao 6.3, na ordem em que ela as declara. Saem dos campos de
# Ocorrencia para o cabecalho nunca discordar do conteudo.
COLUNAS_QUALIDADE = tuple(campo.name for campo in fields(Ocorrencia))


def escrever_relatorio_qualidade(
    ocorrencias: list[Ocorrencia], destino: Path
) -> int:
    """
    Grava 'saida/relatorio_qualidade.csv' e devolve quantas linhas escreveu.

    Uma linha por ocorrencia, com as sete colunas da secao 6.3. E o arquivo em
    que a Decisao 2 se materializa: sem ele, 'nada some sem deixar rastro' seria
    uma frase no README, porque o rastro so existiria em memoria durante a
    execucao.

    O arquivo e gravado mesmo sem nenhuma ocorrencia. Um CSV com so o cabecalho
    diz 'a importacao rodou e nao achou nada'; a ausencia do arquivo e
    indistinguivel de 'a importacao nao rodou'.
    """
    destino = Path(destino)
    try:
        destino.parent.mkdir(parents=True, exist_ok=True)
        with destino.open("w", encoding=CODIFICACAO_CSV, newline="") as arquivo:
            escritor = csv.writer(arquivo, delimiter=DELIMITADOR_CSV)
            escritor.writerow(COLUNAS_QUALIDADE)
            for ocorrencia in ocorrencias:
                escritor.writerow(
                    [getattr(ocorrencia, coluna) for coluna in COLUNAS_QUALIDADE]
                )
    except OSError as erro:
        raise ErroValidacao(
            f"nao foi possivel gravar o relatorio de qualidade em {destino}: {erro}"
        ) from erro

    return len(ocorrencias)


# ---------------------------------------------------------------------------
# Aplicacao da limpeza, celula a celula
# ---------------------------------------------------------------------------

def limpadores(cfg: Configuracao) -> dict[str, Callable[[Any], ValorLimpo]]:
    """
    Diz qual funcao de limpeza trata cada coluna.

    O casamento e conferido nos dois sentidos contra 'colunas' do config. Coluna
    do config sem limpador chegaria crua ao banco - serial do Excel virando
    string de data; limpador sem coluna e regra que nunca dispara. Nos dois casos
    o efeito seria silencioso, entao o carregamento recusa.
    """
    por_coluna: dict[str, Callable[[Any], ValorLimpo]] = {
        "numero_os": normalizar_texto,
        "cliente": normalizar_texto,
        "produto": normalizar_texto,
        "operador": normalizar_texto,
        "maquina": lambda valor: normalizar_maquina(valor, cfg.maquinas),
        "turno": lambda valor: normalizar_turno(valor, cfg.turnos),
        "status": lambda valor: normalizar_status(valor, cfg.status),
        "data_entrada": converter_data,
        "data_prazo": converter_data,
        "data_entrega": converter_data,
    }
    # As colunas numericas saem de TIPO_NUMERICO_POR_COLUNA para o tipo esperado
    # vir de um lugar so. O 'tipo=tipo' fixa o valor no momento em que a lambda e
    # criada; sem ele todas fechariam sobre a ultima volta do laco.
    for coluna, tipo in TIPO_NUMERICO_POR_COLUNA.items():
        por_coluna[coluna] = lambda valor, tipo=tipo: converter_numero(valor, tipo)

    sem_limpador = sorted(set(cfg.colunas) - set(por_coluna))
    if sem_limpador:
        raise ErroValidacao(
            f"o config.yaml declara colunas que a validacao nao sabe limpar: "
            f"{', '.join(sem_limpador)}. Acrescente o limpador em "
            f"validacao.limpadores"
        )
    sem_coluna = sorted(set(por_coluna) - set(cfg.colunas))
    if sem_coluna:
        raise ErroValidacao(
            f"a validacao tem limpador para colunas que o config.yaml nao declara "
            f"em 'colunas': {', '.join(sem_coluna)}. "
            f"Declaradas: {', '.join(sorted(cfg.colunas))}"
        )
    return por_coluna


def limpar_linha(
    linha: dict[str, Any],
    por_coluna: dict[str, Callable[[Any], ValorLimpo]],
) -> tuple[dict[str, Any], list[Ocorrencia], list[tuple[str, str]]]:
    """
    Passa cada celula da linha pela sua funcao de limpeza.

    Devolve tres coisas: os valores limpos, as ocorrencias de correcao, e a lista
    de (coluna, problema) das celulas que nao deu para limpar.

    O valor que nao deu para limpar vira None nos valores limpos e o motivo sobe
    para quem chamou decidir. A escolha deste pipeline e rejeitar a linha inteira
    (veja validar_linha): gravar a ordem com a celula em branco faria cada
    indicador rodar sobre um denominador diferente, sem nada na tela dizendo isso.
    """
    arquivo = linha[COLUNA_ARQUIVO]
    numero = linha[COLUNA_LINHA]

    valores: dict[str, Any] = {COLUNA_ARQUIVO: arquivo, COLUNA_LINHA: numero}
    correcoes: list[Ocorrencia] = []
    ilegiveis: list[tuple[str, str]] = []

    for coluna, limpar in por_coluna.items():
        bruto = linha[coluna]
        limpo = limpar(bruto)
        valores[coluna] = limpo.valor

        if limpo.problema is not None:
            ilegiveis.append((coluna, limpo.problema))
        elif limpo.correcao is not None:
            correcoes.append(
                Ocorrencia(
                    arquivo=arquivo,
                    linha_planilha=numero,
                    coluna=coluna,
                    valor_original=_texto(bruto),
                    problema=limpo.correcao,
                    acao=ACAO_CORRIGIDO,
                    valor_final=_texto(limpo.valor),
                )
            )

    return valores, correcoes, ilegiveis


# ---------------------------------------------------------------------------
# Regras de negocio
#
# Cada regra recebe os valores ja limpos e rende (coluna, motivo, problema) para
# cada violacao. Regra que nao tem o dado de que precisa nao rende nada: celula
# vazia e assunto da regra de obrigatorios, e reclamar duas vezes da mesma
# ausencia so polui o relatorio.
# ---------------------------------------------------------------------------

Violacao = tuple[str, str, str]

# As duas colunas de tempo, na ordem em que a ordem acontece: primeiro acerta a
# maquina, depois roda. A ordem importa para a mensagem da regra do tempo total.
TEMPOS = ("tempo_setup_min", "tempo_producao_min")


def _regra_obrigatorios(valores: dict, cfg: Configuracao) -> Iterator[Violacao]:
    """Campos de 'validacao.obrigatorios' precisam estar preenchidos."""
    for coluna in cfg.validacao["obrigatorios"]:
        if valores[coluna] is None:
            yield coluna, MOTIVO_OBRIGATORIO_VAZIO, (
                f"campo obrigatorio {coluna!r} vazio ou ilegivel"
            )


def _regra_tiragem_boa(valores: dict, cfg: Configuracao) -> Iterator[Violacao]:
    """
    Ordem que rodou precisa dizer quanta folha boa saiu.

    'tiragem_boa' nao esta em 'validacao.obrigatorios' porque a exigencia e
    condicional, e lista plana nao expressa condicao: a ordem CANCELADA e
    legitimamente zero, e o gerador a produz assim. Fora dela, tiragem boa vazia
    tira a ordem da produtividade e do refugo ao mesmo tempo.

    Status vazio cai aqui tambem, e e o certo: sem status nao ha como afirmar que
    a ordem foi cancelada.
    """
    if valores["tiragem_boa"] is None and valores["status"] != STATUS_CANCELADA:
        yield "tiragem_boa", MOTIVO_TIRAGEM_BOA_VAZIA, (
            f"tiragem boa vazia com status {_texto(valores['status']) or '(vazio)'}: "
            f"sem ela a ordem nao tem produtividade nem refugo"
        )


def _regra_negativos(valores: dict, cfg: Configuracao) -> Iterator[Violacao]:
    """
    Nenhuma quantidade nem tempo pode ser negativo.

    A especificacao pede so 'tempos nao negativos'. A regra cobre as cinco
    colunas numericas porque tiragem e refugo negativos sao igualmente
    impossiveis, e deixar passar produziria refugo percentual negativo no painel.
    """
    for coluna in TIPO_NUMERICO_POR_COLUNA:
        valor = valores[coluna]
        if valor is not None and valor < 0:
            yield coluna, MOTIVO_VALOR_NEGATIVO, (
                f"{coluna} {valor} e negativo"
            )


def _regra_tempo_maximo(valores: dict, cfg: Configuracao) -> Iterator[Violacao]:
    """
    Nenhum tempo isolado passa de 'validacao.tempo_maximo_minutos'.

    E o mesmo uso que config.py ja faz da chave: ela e o teto das faixas de
    'gerador.setup_por_maquina'. Pega o acerto ou a producao absurdo sozinho,
    mesmo quando o outro esta zerado e a soma nao denuncia nada.
    """
    maximo = cfg.validacao["tempo_maximo_minutos"]
    for coluna in TEMPOS:
        valor = valores[coluna]
        if valor is not None and valor > maximo:
            yield coluna, MOTIVO_TEMPO_ACIMA_DO_MAXIMO, (
                f"{coluna} de {valor} min passa do maximo de {maximo} min por coluna"
            )


def _regra_tempo_total(valores: dict, cfg: Configuracao) -> Iterator[Violacao]:
    """
    Acerto mais producao nao passam de 'validacao.tempo_maximo_minutos'.

    E a leitura literal do comentario da chave no config.yaml - '48h numa unica
    ordem' e o tempo que a ordem ocupou a maquina, nao o de cada etapa. A regra
    por coluna sozinha deixava passar 40h de producao com 20h de acerto: cada
    metade cabe no teto e a ordem, somada, ocupa 60h.

    Tempo negativo nao chega aqui: a soma com um valor impossivel nao significa
    nada, e _regra_negativos ja rejeitou a linha nomeando a celula certa.
    """
    tempos = [valores[coluna] for coluna in TEMPOS]
    if any(tempo is None or tempo < 0 for tempo in tempos):
        return

    maximo = cfg.validacao["tempo_maximo_minutos"]
    total = sum(tempos)
    if total > maximo:
        yield "tempo_producao_min", MOTIVO_TEMPO_TOTAL_ACIMA_DO_MAXIMO, (
            f"acerto de {tempos[0]} min mais producao de {tempos[1]} min somam "
            f"{total} min e passam do maximo de {maximo} min por ordem"
        )


def _regra_refugo(valores: dict, cfg: Configuracao) -> Iterator[Violacao]:
    """
    Refugo nao passa de 'refugo_maximo_sobre_tiragem' da tiragem prevista.

    A base e a tiragem PREVISTA e nao a boa, de proposito: quando a ordem
    refuga quase tudo, a tiragem boa despenca junto e um limite sobre ela se
    ajustaria ao proprio desastre. O pedido do cliente nao se ajusta.
    """
    prevista, refugo = valores["tiragem_prevista"], valores["refugo"]
    if prevista is None or refugo is None or refugo < 0:
        return

    fracao = cfg.validacao["refugo_maximo_sobre_tiragem"]
    limite = prevista * fracao
    if refugo > limite:
        yield "refugo", MOTIVO_REFUGO_ACIMA_DO_LIMITE, (
            f"refugo de {refugo} passa de {fracao:.0%} da tiragem prevista de "
            f"{prevista} (limite {limite:.0f})"
        )


def _regra_datas(valores: dict, cfg: Configuracao) -> Iterator[Violacao]:
    """Prazo e entrega nao podem ser anteriores a entrada em producao."""
    entrada = valores["data_entrada"]
    if entrada is None:
        return

    motivos = {
        "data_prazo": MOTIVO_PRAZO_ANTES_DA_ENTRADA,
        "data_entrega": MOTIVO_ENTREGA_ANTES_DA_ENTRADA,
    }
    for coluna, motivo in motivos.items():
        valor = valores[coluna]
        if valor is not None and valor < entrada:
            yield coluna, motivo, (
                f"{coluna} {valor.isoformat()} e anterior a data_entrada "
                f"{entrada.isoformat()}"
            )


def _regra_status_entrega(valores: dict, cfg: Configuracao) -> Iterator[Violacao]:
    """
    Status e data de entrega precisam contar a mesma historia.

    A ausencia de data de entrega tem um significado so: a ordem nao foi
    entregue. Entao ENTREGUE sem data e nao entregue com data sao contradicoes, e
    nao ha como saber qual das duas celulas esta certa - inventar a data faria o
    OTD depender de um chute, e apagar a data em silencio faria o painel afirmar
    uma entrega que nao existiu.
    """
    status, entrega = valores["status"], valores["data_entrega"]
    if status is None:
        return

    if status == STATUS_ENTREGUE and entrega is None:
        yield "data_entrega", MOTIVO_STATUS_INCOERENTE, (
            f"status {STATUS_ENTREGUE} sem data de entrega: a ordem nao teria "
            f"como entrar no calculo de OTD"
        )
    elif status != STATUS_ENTREGUE and entrega is not None:
        yield "data_entrega", MOTIVO_STATUS_INCOERENTE, (
            f"status {status} com data de entrega {entrega.isoformat()}: so ordem "
            f"{STATUS_ENTREGUE} tem data de entrega"
        )


# A ordem importa so para o relatorio ficar legivel: o que falta vem antes do
# que esta errado.
REGRAS: tuple[Callable[[dict, Configuracao], Iterator[Violacao]], ...] = (
    _regra_obrigatorios,
    _regra_tiragem_boa,
    _regra_negativos,
    _regra_tempo_maximo,
    _regra_tempo_total,
    _regra_refugo,
    _regra_datas,
    _regra_status_entrega,
)


def validar_linha(
    linha: dict[str, Any],
    por_coluna: dict[str, Callable[[Any], ValorLimpo]],
    cfg: Configuracao,
) -> tuple[dict[str, Any], list[Ocorrencia], list[str]]:
    """
    Limpa e valida uma linha.

    Devolve os valores limpos, as ocorrencias que a linha gerou e a lista de
    motivos de rejeicao - vazia quando a linha foi aprovada.

    Celula ilegivel rejeita a linha inteira. A alternativa - gravar a ordem com
    a celula em branco e tratar o desconhecido como membro explicito da
    dimensao, '(nao informado)' - esta registrada na secao 4.4 da especificacao
    junto com o motivo de nao ter sido adotada e o que essa escolha custa.

    Todas as regras rodam mesmo depois da primeira falha. Devolver so o primeiro
    motivo obrigaria a corrigir a planilha, rodar de novo e descobrir o segundo.
    """
    valores, ocorrencias, ilegiveis = limpar_linha(linha, por_coluna)

    violacoes: list[Violacao] = [
        (coluna, MOTIVO_ILEGIVEL, problema) for coluna, problema in ilegiveis
    ]
    for regra in REGRAS:
        violacoes.extend(regra(valores, cfg))

    motivos: list[str] = []
    for coluna, motivo, problema in violacoes:
        if motivo not in motivos:
            motivos.append(motivo)
        ocorrencias.append(
            Ocorrencia(
                arquivo=linha[COLUNA_ARQUIVO],
                linha_planilha=linha[COLUNA_LINHA],
                coluna=coluna,
                valor_original=_texto(linha[coluna]),
                problema=problema,
                acao=ACAO_REJEITADO,
                valor_final="",
            )
        )

    return valores, ocorrencias, motivos


# ---------------------------------------------------------------------------
# Orquestracao
# ---------------------------------------------------------------------------

def _conferir_status_conhecidos(cfg: Configuracao) -> None:
    """
    Confere que os status consultados pelas regras existem no config.

    Sem isso, renomear 'ENTREGUE' no config.yaml desligaria a regra de coerencia
    e a de tiragem boa de uma vez, sem erro nenhum: as comparacoes simplesmente
    nunca dariam verdadeiro.
    """
    faltando = [s for s in STATUS_EXIGIDOS if s not in cfg.status]
    if faltando:
        raise ErroValidacao(
            f"a secao 'status' do config nao declara "
            f"{', '.join(repr(s) for s in faltando)}, que as regras de validacao "
            f"consultam pelo significado. Declarados: {', '.join(sorted(cfg.status))}"
        )


def ocorrencias_de_descartes(descartes: list[Descarte]) -> list[Ocorrencia]:
    """
    Converte os descartes da leitura em linhas do relatorio de qualidade.

    Linha em branco e linha TOTAL de rodape nao sao ordem de producao, entao a
    acao e 'descartado' e elas ficam fora da taxa de rejeicao. Mas precisam
    aparecer no relatorio: o rodape somado como se fosse ordem e um jeito
    classico de o total do painel sair errado, e quem le o CSV tem que poder
    conferir que ele foi removido de proposito.
    """
    return [
        Ocorrencia(
            arquivo=descarte.arquivo,
            linha_planilha=descarte.linha_planilha,
            coluna="",
            valor_original=descarte.conteudo,
            problema=descarte.motivo,
            acao=ACAO_DESCARTADO,
            valor_final="",
        )
        for descarte in descartes
    ]


def validar(dados: pd.DataFrame, cfg: Configuracao) -> ResultadoValidacao:
    """
    Limpa e valida o DataFrame cru da leitura.

    Recebe o DataFrame, e nao o ResultadoLeitura inteiro, para dar para testar
    com uma tabela escrita a mao. Os descartes da leitura entram no relatorio por
    ocorrencias_de_descartes, que quem orquestra chama e concatena.
    """
    _conferir_status_conhecidos(cfg)
    por_coluna = limpadores(cfg)

    colunas_saida = list(cfg.colunas) + [COLUNA_ARQUIVO, COLUNA_LINHA]

    aprovadas: list[dict[str, Any]] = []
    rejeitadas: list[dict[str, Any]] = []
    ocorrencias: list[Ocorrencia] = []
    linhas_por_motivo: dict[str, int] = {}
    linhas_corrigidas = 0

    for linha in dados.to_dict("records"):
        valores, da_linha, motivos = validar_linha(linha, por_coluna, cfg)
        ocorrencias.extend(da_linha)

        if any(o.acao == ACAO_CORRIGIDO for o in da_linha):
            linhas_corrigidas += 1

        if motivos:
            for motivo in motivos:
                linhas_por_motivo[motivo] = linhas_por_motivo.get(motivo, 0) + 1
            rejeitadas.append({**valores, COLUNA_MOTIVOS: "; ".join(motivos)})
        else:
            aprovadas.append(valores)

    return ResultadoValidacao(
        aprovadas=pd.DataFrame(aprovadas, columns=colunas_saida, dtype=object),
        rejeitadas=pd.DataFrame(
            rejeitadas, columns=colunas_saida + [COLUNA_MOTIVOS], dtype=object
        ),
        ocorrencias=ocorrencias,
        total_linhas=len(dados),
        linhas_por_motivo=linhas_por_motivo,
        linhas_corrigidas=linhas_corrigidas,
    )


def descrever_resultado(resultado: ResultadoValidacao, cfg: Configuracao) -> str:
    """Monta o relatorio legivel da validacao."""
    limite = cfg.validacao["taxa_rejeicao_limite"]
    linhas = [
        "Validacao",
        f"  linhas de dado lidas   {resultado.total_linhas:>5}",
        f"  aprovadas              {len(resultado.aprovadas):>5}"
        f"  {len(resultado.aprovadas) / max(resultado.total_linhas, 1):>7.2%}",
        f"  rejeitadas             {resultado.linhas_rejeitadas:>5}"
        f"  {resultado.taxa_rejeicao:>7.2%}",
        f"  linhas com correcao    {resultado.linhas_corrigidas:>5}"
        f"  {resultado.linhas_corrigidas / max(resultado.total_linhas, 1):>7.2%}",
        "",
        f"  limite de rejeicao: {limite:.0%}   "
        + (
            "ACIMA DO LIMITE - o programa sai com codigo 1"
            if resultado.acima_do_limite(cfg)
            else "dentro do limite"
        ),
    ]

    if resultado.linhas_por_motivo:
        linhas.append("")
        linhas.append("Rejeicao por motivo, em linhas")
        # A soma pode passar do total de rejeitadas: uma linha com refugo
        # impossivel E entrega antes da entrada conta nos dois motivos. Somar as
        # colunas para conferir o total daria um numero maior que 100%.
        for motivo in sorted(
            resultado.linhas_por_motivo, key=lambda m: (-resultado.linhas_por_motivo[m], m)
        ):
            quantas = resultado.linhas_por_motivo[motivo]
            linhas.append(
                f"  {motivo:<42} {quantas:>4}  {quantas / resultado.total_linhas:>6.2%}"
            )
        soma = sum(resultado.linhas_por_motivo.values())
        if soma != resultado.linhas_rejeitadas:
            linhas.append(
                f"  {'(soma dos motivos)':<42} {soma:>4}  "
                f"{soma - resultado.linhas_rejeitadas} linha(s) violam mais de uma regra"
            )

    por_acao: dict[str, int] = {}
    for ocorrencia in resultado.ocorrencias:
        por_acao[ocorrencia.acao] = por_acao.get(ocorrencia.acao, 0) + 1
    if por_acao:
        linhas.append("")
        linhas.append(f"Relatorio de qualidade ({len(resultado.ocorrencias)} ocorrencias)")
        for acao in sorted(por_acao):
            linhas.append(f"  {acao:<42} {por_acao[acao]:>4}")

    return "\n".join(linhas)


def _detalhar(resultado: ResultadoValidacao, quantas: int) -> str:
    """Lista as primeiras ocorrencias, para conferir motivo a motivo."""
    linhas = [f"Primeiras {quantas} ocorrencias"]
    for ocorrencia in resultado.ocorrencias[:quantas]:
        coluna = f" {ocorrencia.coluna}" if ocorrencia.coluna else ""
        original = (
            f" {ocorrencia.valor_original!r} ->" if ocorrencia.valor_original else ""
        )
        final = f" {ocorrencia.valor_final!r}" if ocorrencia.valor_final else ""
        linhas.append(
            f"  [{ocorrencia.acao}] {ocorrencia.arquivo} linha "
            f"{ocorrencia.linha_planilha}{coluna}:{original}{final}"
        )
        linhas.append(f"      {ocorrencia.problema}")
    return "\n".join(linhas)


def _argumentos(argv: list[str]) -> argparse.Namespace:
    analisador = argparse.ArgumentParser(
        prog="python -m src.validacao",
        description="Le, limpa e valida as planilhas, e informa a taxa de rejeicao.",
    )
    analisador.add_argument(
        "--config", default=str(CONFIG_PADRAO), help="caminho do config.yaml"
    )
    analisador.add_argument("--entrada", help="sobrepoe caminhos.entrada")
    analisador.add_argument(
        "--leitura", action="store_true", help="mostra tambem o relatorio da leitura"
    )
    analisador.add_argument(
        "--detalhe", type=int, default=0, help="lista as N primeiras ocorrencias"
    )
    return analisador.parse_args(argv)


def main(argv: list[str]) -> int:
    """
    Entrada de 'python -m src.validacao'.

    Sai com 1 quando a taxa de rejeicao passa do limite, como manda a Decisao 2.
    O programa completo repete essa trava no Bloco 10; ela esta aqui para o bloco
    ser conferivel sozinho.
    """
    args = _argumentos(argv)

    try:
        cfg = carregar(args.config)
    except ErroConfiguracao as erro:
        print(f"ERRO DE CONFIGURACAO: {erro}", file=sys.stderr)
        return CODIGO_SAIDA_ERRO_CONFIG

    try:
        leitura = ler_pasta(cfg, args.entrada)
    except ErroLeitura as erro:
        print(f"ERRO DE LEITURA: {erro}", file=sys.stderr)
        return 1

    try:
        resultado = validar(leitura.dados, cfg)
    except ErroValidacao as erro:
        print(f"ERRO DE VALIDACAO: {erro}", file=sys.stderr)
        return 1

    resultado.ocorrencias.extend(ocorrencias_de_descartes(leitura.descartes))

    if args.leitura:
        print(descrever_leitura(leitura))
        print("")

    print(descrever_resultado(resultado, cfg))

    if args.detalhe > 0:
        print("")
        print(_detalhar(resultado, args.detalhe))

    if resultado.acima_do_limite(cfg):
        print(
            f"\nTaxa de rejeicao de {resultado.taxa_rejeicao:.2%} passa do limite de "
            f"{cfg.validacao['taxa_rejeicao_limite']:.0%} configurado em "
            f"'validacao.taxa_rejeicao_limite'",
            file=sys.stderr,
        )
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
