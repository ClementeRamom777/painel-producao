"""
Orquestracao: os subcomandos da secao 8, o logging e os codigos de saida.

Este e o unico modulo que o usuario final precisa conhecer. Os outros tem CLI
proprio, mas aqueles existem para conferir um bloco de cada vez; e aqui que o
pipeline vira um programa.

Tres responsabilidades, e so tres:

1. Traduzir a linha de comando em chamadas aos modulos, sem regra de negocio
   propria. Nenhum calculo acontece aqui - se algo precisar ser decidido sobre
   os dados, o lugar e o modulo da camada, nao o orquestrador.

2. Configurar o logging da secao 6.4 e escrever 'saida/relatorio_qualidade.csv'
   da secao 6.3. Sao as duas saidas que existem para tornar a execucao
   auditavel depois que ela terminou.

3. Terminar com o codigo de saida certo: 0 sucesso, 1 erro de execucao ou
   rejeicao acima do limite, 2 erro de configuracao. Nenhuma excecao escapa
   para o terminal como traceback - ela vai inteira para o log e sai como uma
   linha legivel na tela.

Sobre a rejeicao acima do limite: o programa completa o trabalho e SO ENTAO sai
com 1. As linhas aprovadas continuam sendo dado bom, e o painel gerado mostra a
propria taxa de rejeicao no rodape - que e exatamente a tela que alguem precisa
ver quando a entrada degradou. Abortar no meio esconderia o diagnostico junto
com o problema.
"""

import argparse
import logging
import sys
from datetime import date
from pathlib import Path

from src.banco import (
    ErroBanco,
    ResultadoImportacao,
    conectar,
    descrever_importacao,
    importar,
)
from src.config import (
    CODIGO_SAIDA_ERRO_CONFIG,
    CONFIG_PADRAO,
    Configuracao,
    ErroConfiguracao,
    carregar,
)
from src.gerador_dados import DATA_FIM_PADRAO, ErroGeracao, descrever_resumo, gerar
from src.indicadores import (
    ErroIndicadores,
    ResultadoIndicadores,
    calcular,
    descrever,
    descrever_conferencia,
    exportar_conferencia,
)
from src.leitura import ErroLeitura
from src.painel import ErroPainel
from src.painel import gerar as gerar_painel
from src.relatorio_excel import ErroRelatorio
from src.relatorio_excel import gerar as gerar_relatorio
from src.validacao import ErroValidacao, escrever_relatorio_qualidade

CODIGO_SUCESSO = 0
CODIGO_ERRO = 1

ARQUIVO_QUALIDADE = "relatorio_qualidade.csv"

# Formato da secao 6.4: data hora | nivel | modulo | mensagem.
FORMATO_LOG = "%(asctime)s | %(levelname)-8s | %(name)-18s | %(message)s"
FORMATO_DATA_LOG = "%Y-%m-%d %H:%M:%S"

# Erros esperados de cada camada. Todos significam a mesma coisa para quem
# chamou - a execucao falhou por um motivo que o programa sabe explicar - e por
# isso saem com o mesmo codigo. O que os distingue e a mensagem, nao o codigo.
ERROS_DE_EXECUCAO = (
    ErroGeracao,
    ErroLeitura,
    ErroValidacao,
    ErroBanco,
    ErroIndicadores,
    ErroPainel,
    ErroRelatorio,
)

log = logging.getLogger("main")


class FormatadorSemPilha(logging.Formatter):
    """
    Formatador do terminal: mostra a mensagem, nunca a pilha.

    'log.exception' entrega o traceback a TODOS os handlers, entao sem isto a
    pilha apareceria na tela apesar de o modulo prometer o contrario. A pilha
    continua indo inteira para o arquivo, que e onde ela serve.

    A copia do registro nao e capricho. O Formatter guarda o traceback ja
    formatado em 'record.exc_text' e o reaproveita: como o handler do arquivo
    formata primeiro, o campo chega aqui preenchido, e apagar a pilha do
    registro original a apagaria tambem do log em disco.
    """

    def format(self, record: logging.LogRecord) -> str:
        copia = logging.makeLogRecord(record.__dict__)
        copia.exc_info = None
        copia.exc_text = None
        copia.stack_info = None
        return super().format(copia)


def configurar_log(cfg: Configuracao, verbose: bool) -> None:
    """
    Liga o log em arquivo e no terminal, nos niveis da secao 6.4.

    Os dois destinos tem niveis diferentes de proposito. O arquivo recebe tudo
    a partir do nivel configurado, porque e nele que se investiga depois. O
    terminal so recebe AVISO para cima, porque cada subcomando ja imprime o
    proprio relatorio e misturar log com relatorio deixa os dois ilegiveis.

    '--verbose' iguala os dois em DEBUG: e o modo de descobrir por que uma linha
    especifica foi parar onde foi.
    """
    nivel_arquivo = logging.DEBUG if verbose else cfg.log["nivel"].upper()
    nivel_terminal = logging.DEBUG if verbose else logging.WARNING

    raiz = logging.getLogger()
    raiz.setLevel(logging.DEBUG)
    # Sem isso, chamar main() duas vezes no mesmo processo - como o teste faz -
    # empilharia um handler por chamada e duplicaria cada linha do log.
    for handler in list(raiz.handlers):
        raiz.removeHandler(handler)
        handler.close()

    formatador = logging.Formatter(FORMATO_LOG, datefmt=FORMATO_DATA_LOG)

    arquivo = logging.FileHandler(cfg.arquivo_log(), encoding="utf-8")
    arquivo.setLevel(nivel_arquivo)
    arquivo.setFormatter(formatador)
    raiz.addHandler(arquivo)

    terminal = logging.StreamHandler(sys.stderr)
    terminal.setLevel(nivel_terminal)
    terminal.setFormatter(FormatadorSemPilha(FORMATO_LOG, datefmt=FORMATO_DATA_LOG))
    raiz.addHandler(terminal)


def _caminho(cfg: Configuracao, chave: str, sobreposicao: str | None) -> Path:
    """Resolve um caminho do config, respeitando a sobreposicao da linha de comando."""
    return cfg.caminho(chave) if sobreposicao is None else Path(sobreposicao)


def _data_valida(rotulo: str, texto: str | None) -> str | None:
    """Confere que o texto e uma data ISO antes de ela entrar numa consulta."""
    if texto is None:
        return None
    try:
        date.fromisoformat(texto)
    except ValueError as erro:
        raise ErroIndicadores(
            f"{rotulo} {texto!r} nao e uma data no formato AAAA-MM-DD ({erro})"
        ) from erro
    return texto


# ---------------------------------------------------------------------------
# Subcomandos
# ---------------------------------------------------------------------------

def comando_gerar_dados(cfg: Configuracao, args: argparse.Namespace) -> int:
    """Gera as planilhas sinteticas na pasta de entrada."""
    destino = _caminho(cfg, "entrada", args.entrada)
    log.info("gerando planilhas sinteticas em %s", destino)

    resumo = gerar(
        cfg,
        meses=args.meses,
        arquivos=args.arquivos,
        data_fim=date.fromisoformat(args.fim),
        semente=args.semente,
        destino=destino,
    )
    print(descrever_resumo(resumo, cfg, destino))
    log.info(
        "geradas %d ordens em %d arquivo(s)", resumo.ordens_limpas, len(resumo.arquivos)
    )
    return CODIGO_SUCESSO


def _executar_importacao(
    cfg: Configuracao, args: argparse.Namespace
) -> ResultadoImportacao:
    """
    Le, limpa, valida, grava e deixa o rastro em disco.

    Devolve o resultado para quem chamou decidir o codigo de saida. O 'tudo'
    reaproveita este mesmo resultado no relatorio Excel, para nao importar duas
    vezes so por causa da aba de qualidade.
    """
    entrada = _caminho(cfg, "entrada", args.entrada)
    banco = _caminho(cfg, "banco", args.banco)
    log.info("importando de %s para %s", entrada, banco)

    resultado = importar(cfg, entrada=entrada, banco=banco)

    destino = _caminho(cfg, "saida", None) / ARQUIVO_QUALIDADE
    escritas = escrever_relatorio_qualidade(resultado.ocorrencias, destino)

    validacao = resultado.validacao
    log.info(
        "lidas %d, aprovadas %d, rejeitadas %d (%.2f%%), gravadas %d",
        validacao.total_linhas,
        len(validacao.aprovadas),
        validacao.linhas_rejeitadas,
        validacao.taxa_rejeicao * 100,
        resultado.gravacao.gravadas,
    )
    if resultado.gravacao.conflitos:
        log.warning(
            "%d linha(s) com a mesma chave natural e conteudo diferente: "
            "veja a acao 'conflito' em %s",
            resultado.gravacao.conflitos,
            destino,
        )
    if validacao.acima_do_limite(cfg):
        log.error(
            "taxa de rejeicao de %.2f%% passa do limite de %.0f%% em "
            "'validacao.taxa_rejeicao_limite'",
            validacao.taxa_rejeicao * 100,
            cfg.validacao["taxa_rejeicao_limite"] * 100,
        )

    print(f"Relatorio de qualidade: {escritas} ocorrencia(s) em {destino}")
    return resultado


def comando_importar(cfg: Configuracao, args: argparse.Namespace) -> int:
    """Importa as planilhas da pasta de entrada para o banco."""
    resultado = _executar_importacao(cfg, args)
    print(descrever_importacao(resultado, cfg))
    return _codigo_da_rejeicao(cfg, resultado)


def _codigo_da_rejeicao(cfg: Configuracao, resultado: ResultadoImportacao) -> int:
    """Traduz a taxa de rejeicao no codigo de saida da Decisao 2."""
    if not resultado.validacao.acima_do_limite(cfg):
        return CODIGO_SUCESSO
    print(
        f"\nTaxa de rejeicao de {resultado.validacao.taxa_rejeicao:.2%} passa do "
        f"limite de {cfg.validacao['taxa_rejeicao_limite']:.0%} configurado em "
        f"'validacao.taxa_rejeicao_limite'",
        file=sys.stderr,
    )
    return CODIGO_ERRO


def _imprimir_indicadores(
    cfg: Configuracao,
    args: argparse.Namespace,
    maquina: str | None = None,
    csv: str | None = None,
) -> ResultadoIndicadores:
    """
    Calcula e imprime os indicadores.

    Separado de comando_indicadores porque o 'tudo' tambem precisa imprimi-los,
    e ele nao declara '--maquina' nem '--csv'. Passar os dois como parametro, em
    vez de ler de args, e o que permite as duas chamadas sem que uma delas
    dependa de um atributo que o Namespace da outra nao tem.
    """
    banco = _caminho(cfg, "banco", args.banco)
    if not banco.is_file():
        raise ErroIndicadores(
            f"banco nao encontrado em {banco}. Rode 'importar' antes"
        )

    de = _data_valida("--de", args.de)
    ate = _data_valida("--ate", args.ate)

    conexao = conectar(banco)
    try:
        resultado = calcular(conexao, de, ate)
        print(descrever(resultado, cfg))

        if maquina:
            print("")
            print(descrever_conferencia(maquina, resultado, cfg))
            if csv:
                gravadas = exportar_conferencia(conexao, maquina, resultado, Path(csv))
                print("")
                print(f"  {gravadas} ordens gravadas em {csv}")
    finally:
        conexao.close()

    log.info(
        "indicadores de %s a %s sobre %d ordens",
        resultado.de, resultado.ate, resultado.total.ordens,
    )
    return resultado


def comando_indicadores(cfg: Configuracao, args: argparse.Namespace) -> int:
    """Calcula e imprime os indicadores do periodo."""
    _imprimir_indicadores(cfg, args, maquina=args.maquina, csv=args.csv)
    return CODIGO_SUCESSO


def comando_painel(cfg: Configuracao, args: argparse.Namespace) -> int:
    """Gera o painel HTML autocontido."""
    destino, tamanho = gerar_painel(
        cfg,
        banco=_caminho(cfg, "banco", args.banco),
        de=_data_valida("--de", args.de),
        ate=_data_valida("--ate", args.ate),
        destino=args.saida,
    )
    print(f"Painel gravado em {destino}")
    print(f"Tamanho: {tamanho / 1024:.0f} KB, autocontido - abre sem internet")
    log.info("painel gravado em %s (%d bytes)", destino, tamanho)
    return CODIGO_SUCESSO


def comando_relatorio(cfg: Configuracao, args: argparse.Namespace) -> int:
    """
    Gera o relatorio Excel.

    Sem ocorrencias em maos, o modulo importa por conta propria para preencher a
    aba de qualidade - o banco guarda so a contagem agregada, nao a ocorrencia
    linha a linha. A importacao e idempotente, entao isso nao altera o banco.
    """
    destino, ordens = gerar_relatorio(
        cfg,
        banco=_caminho(cfg, "banco", args.banco),
        entrada=_caminho(cfg, "entrada", args.entrada),
        de=_data_valida("--de", args.de),
        ate=_data_valida("--ate", args.ate),
        destino=args.saida,
    )
    print(f"Relatorio gravado em {destino}")
    print(f"{ordens} ordens no periodo, em 5 abas")
    log.info("relatorio gravado em %s", destino)
    return CODIGO_SUCESSO


def comando_tudo(cfg: Configuracao, args: argparse.Namespace) -> int:
    """
    Da geracao ao relatorio, num comando.

    A importacao roda uma vez so: as ocorrencias dela sao passadas adiante para
    o relatorio Excel, que sem elas importaria de novo.

    A rejeicao acima do limite nao interrompe a sequencia. O painel e o
    relatorio saem, mostrando a taxa alta no rodape e na aba de qualidade, e o
    codigo de saida 1 avisa o agendador. Parar no meio deixaria quem investiga
    sem as duas telas que explicam o que aconteceu.
    """
    etapas = []

    print("== gerar dados ==")
    comando_gerar_dados(cfg, args)
    etapas.append("dados gerados")

    print("\n== importar ==")
    importacao = _executar_importacao(cfg, args)
    print(descrever_importacao(importacao, cfg))
    etapas.append(f"{importacao.gravacao.gravadas} ordens no banco")

    print("\n== indicadores ==")
    _imprimir_indicadores(cfg, args)

    print("\n== painel ==")
    comando_painel(cfg, args)
    etapas.append("painel gerado")

    print("\n== relatorio ==")
    destino, ordens = gerar_relatorio(
        cfg,
        banco=_caminho(cfg, "banco", args.banco),
        de=_data_valida("--de", args.de),
        ate=_data_valida("--ate", args.ate),
        destino=args.saida_relatorio,
        ocorrencias=importacao.ocorrencias,
    )
    print(f"Relatorio gravado em {destino}")
    etapas.append("relatorio gerado")

    print("\n== fim ==")
    for etapa in etapas:
        print(f"  {etapa}")
    log.info("execucao completa: %s", "; ".join(etapas))

    return _codigo_da_rejeicao(cfg, importacao)


COMANDOS = {
    "gerar-dados": comando_gerar_dados,
    "importar": comando_importar,
    "indicadores": comando_indicadores,
    "painel": comando_painel,
    "relatorio": comando_relatorio,
    "tudo": comando_tudo,
}


# ---------------------------------------------------------------------------
# Linha de comando
# ---------------------------------------------------------------------------

def construir_analisador() -> argparse.ArgumentParser:
    """
    Monta o argparse com os subcomandos da secao 8.

    '--config' e '--verbose' ficam num analisador pai herdado por todos, porque
    a secao 8 diz que todos os aceitam. Repeti-los em cada subcomando
    funcionaria e sairia de sincronia na primeira mudanca.
    """
    pai = argparse.ArgumentParser(add_help=False)
    pai.add_argument(
        "--config", default=str(CONFIG_PADRAO), help="caminho do config.yaml"
    )
    pai.add_argument(
        "--verbose", action="store_true", help="log em DEBUG, no arquivo e na tela"
    )

    analisador = argparse.ArgumentParser(
        prog="python -m src.main",
        description="Pipeline de producao industrial: planilha suja -> painel.",
        parents=[pai],
    )
    subcomandos = analisador.add_subparsers(dest="comando", metavar="COMANDO")

    gerar = subcomandos.add_parser(
        "gerar-dados", parents=[pai],
        help="gera as planilhas sinteticas com sujeira proposital",
    )
    gerar.add_argument("--meses", type=int, help="sobrepoe gerador.meses")
    gerar.add_argument("--arquivos", type=int, help="sobrepoe gerador.arquivos")
    gerar.add_argument("--semente", type=int, help="sobrepoe gerador.semente")
    gerar.add_argument(
        "--fim", default=DATA_FIM_PADRAO.isoformat(),
        help="ultimo dia da janela de historico, em AAAA-MM-DD",
    )
    gerar.add_argument("--entrada", help="sobrepoe caminhos.entrada")

    importa = subcomandos.add_parser(
        "importar", parents=[pai],
        help="le, limpa, valida e grava as planilhas no banco",
    )
    importa.add_argument("--entrada", help="sobrepoe caminhos.entrada")
    importa.add_argument("--banco", help="sobrepoe caminhos.banco")

    indicadores = subcomandos.add_parser(
        "indicadores", parents=[pai], help="calcula os indicadores do periodo",
    )
    indicadores.add_argument("--banco", help="sobrepoe caminhos.banco")
    indicadores.add_argument("--de", help="inicio do periodo, AAAA-MM-DD")
    indicadores.add_argument("--ate", help="fim do periodo, AAAA-MM-DD")
    indicadores.add_argument(
        "--maquina", help="imprime a conferencia manual desta maquina"
    )
    indicadores.add_argument("--csv", help="grava as ordens da maquina neste arquivo")

    painel = subcomandos.add_parser(
        "painel", parents=[pai], help="gera o painel HTML autocontido",
    )
    painel.add_argument("--banco", help="sobrepoe caminhos.banco")
    painel.add_argument("--saida", help="sobrepoe o arquivo de destino")
    painel.add_argument("--de", help="inicio do periodo, AAAA-MM-DD")
    painel.add_argument("--ate", help="fim do periodo, AAAA-MM-DD")

    relatorio = subcomandos.add_parser(
        "relatorio", parents=[pai], help="gera o relatorio Excel de cinco abas",
    )
    relatorio.add_argument("--banco", help="sobrepoe caminhos.banco")
    relatorio.add_argument("--entrada", help="sobrepoe caminhos.entrada")
    relatorio.add_argument("--saida", help="sobrepoe o arquivo de destino")
    relatorio.add_argument("--de", help="inicio do periodo, AAAA-MM-DD")
    relatorio.add_argument("--ate", help="fim do periodo, AAAA-MM-DD")

    tudo = subcomandos.add_parser(
        "tudo", parents=[pai],
        help="da geracao ao relatorio, num comando",
    )
    tudo.add_argument("--meses", type=int, help="sobrepoe gerador.meses")
    tudo.add_argument("--arquivos", type=int, help="sobrepoe gerador.arquivos")
    tudo.add_argument("--semente", type=int, help="sobrepoe gerador.semente")
    tudo.add_argument(
        "--fim", default=DATA_FIM_PADRAO.isoformat(),
        help="ultimo dia da janela de historico, em AAAA-MM-DD",
    )
    tudo.add_argument("--entrada", help="sobrepoe caminhos.entrada")
    tudo.add_argument("--banco", help="sobrepoe caminhos.banco")
    tudo.add_argument("--saida", help="sobrepoe o arquivo do painel")
    tudo.add_argument(
        "--saida-relatorio", dest="saida_relatorio",
        help="sobrepoe o arquivo do relatorio Excel",
    )
    tudo.add_argument("--de", help="inicio do periodo, AAAA-MM-DD")
    tudo.add_argument("--ate", help="fim do periodo, AAAA-MM-DD")

    return analisador


def main(argv: list[str]) -> int:
    """
    Entrada de 'python -m src.main'.

    Nenhuma excecao escapa daqui como traceback. A esperada vira uma linha de
    erro; a inesperada vai inteira para o log, com pilha, e sai como uma linha
    apontando o arquivo de log - porque quem esta rodando isso num agendador
    precisa saber onde olhar, nao ver a pilha passar na tela.
    """
    analisador = construir_analisador()
    args = analisador.parse_args(argv)

    if args.comando is None:
        analisador.print_help()
        return CODIGO_ERRO

    try:
        cfg = carregar(args.config)
    except ErroConfiguracao as erro:
        print(f"ERRO DE CONFIGURACAO: {erro}", file=sys.stderr)
        return CODIGO_SAIDA_ERRO_CONFIG

    # Antes de abrir o arquivo de log: 'logs/' e 'saida/' estao no .gitignore e
    # nao existem num clone novo.
    cfg.garantir_pastas()
    configurar_log(cfg, args.verbose)
    log.info("comando '%s' com config %s", args.comando, cfg.arquivo)

    try:
        codigo = COMANDOS[args.comando](cfg, args)
    except ERROS_DE_EXECUCAO as erro:
        log.error("%s: %s", type(erro).__name__, erro)
        print(f"ERRO: {erro}", file=sys.stderr)
        return CODIGO_ERRO
    except KeyboardInterrupt:
        log.warning("interrompido pelo usuario")
        print("\nInterrompido.", file=sys.stderr)
        return CODIGO_ERRO
    except Exception as erro:  # noqa: BLE001 - a rede de seguranca do topo
        log.exception("erro inesperado em '%s'", args.comando)
        print(
            f"ERRO INESPERADO: {type(erro).__name__}: {erro}\n"
            f"A pilha completa esta em {cfg.arquivo_log()}",
            file=sys.stderr,
        )
        return CODIGO_ERRO

    log.info("comando '%s' terminou com codigo %d", args.comando, codigo)
    return codigo


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
