"""
Leitura das planilhas de entrada.

Acha a linha do cabecalho real, le com pandas, descarta rodape e linha em
branco, e mapeia os nomes de coluna pelos apelidos do config.yaml.

Duas regras governam este modulo:

1. Nada e descartado em silencio. Toda linha jogada fora vira um Descarte com
   arquivo, numero da linha na planilha e motivo, para alimentar o relatorio de
   qualidade da Decisao 2.

2. Linha com celula obrigatoria vazia NAO e descartada aqui. Ela e dado sujo, e
   dado sujo e assunto da validacao - descartar aqui apagaria justamente a
   ocorrencia que o relatorio precisa mostrar.

Os valores saem crus: data ainda pode vir como serial do Excel, numero como
texto e nome de maquina em qualquer variante. Limpar e o Bloco 4.
"""

import argparse
import sys
from dataclasses import dataclass, field
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

# Primeira celula de uma linha de somatorio no rodape. Nao vem do config porque
# nao e parametro de processo: e o vocabulario do Excel, igual em toda planilha.
MARCADORES_RODAPE = ("TOTAL", "TOTAIS", "SUBTOTAL", "SOMA", "SOMATORIO")

# Colunas de rastreio acrescentadas a cada linha lida. Sem elas o relatorio de
# qualidade nao consegue apontar 'arquivo X, linha 47'.
COLUNA_ARQUIVO = "arquivo_origem"
COLUNA_LINHA = "linha_planilha"

MOTIVO_LINHA_VAZIA = "linha em branco"
MOTIVO_RODAPE = "linha de rodape (somatorio)"


class ErroLeitura(Exception):
    """Planilha ilegivel, sem cabecalho reconhecivel ou sem coluna obrigatoria."""


@dataclass
class Descarte:
    """Uma linha que a leitura jogou fora, com o motivo."""

    arquivo: str
    linha_planilha: int
    motivo: str
    conteudo: str


@dataclass
class ResultadoLeitura:
    """O que a leitura produziu, e tudo que ela precisou decidir no caminho."""

    dados: pd.DataFrame
    arquivos: list[Path] = field(default_factory=list)
    linhas_por_arquivo: dict[str, int] = field(default_factory=dict)
    cabecalho_por_arquivo: dict[str, int] = field(default_factory=dict)
    descartes: list[Descarte] = field(default_factory=list)
    colunas_ausentes: dict[str, list[str]] = field(default_factory=dict)
    colunas_ignoradas: dict[str, list[str]] = field(default_factory=dict)


def normalizar_cabecalho(valor: Any) -> str:
    """
    Poe um nome de coluna no formato de comparacao: maiuscula, espaco unico.

    'setup  (min) ' e 'SETUP (MIN)' precisam casar com o mesmo apelido.
    """
    return " ".join(str(valor).upper().split())


def celula_vazia(valor: Any) -> bool:
    """
    Diz se a celula esta vazia.

    Cobre os tres jeitos de vazio que chegam aqui: None, o NaN que o pandas poe
    em celula em branco, e texto que so tem espaco.
    """
    if valor is None:
        return True
    if isinstance(valor, str):
        return not valor.strip()
    return bool(pd.isna(valor))


def mapa_apelidos(cfg: Configuracao) -> dict[str, str]:
    """
    Monta o indice apelido normalizado -> nome canonico da coluna.

    O config ja foi validado contra apelido repetido em duas colunas, entao aqui
    nao ha ambiguidade a resolver.
    """
    return {
        normalizar_cabecalho(apelido): campo
        for campo, apelidos in cfg.colunas.items()
        for apelido in apelidos
    }


def encontrar_cabecalho(
    bruto: pd.DataFrame,
    apelidos: dict[str, str],
    cfg: Configuracao,
    arquivo: Path,
) -> tuple[int, dict[int, str]]:
    """
    Acha a linha do cabecalho real e o que cada coluna dela significa.

    A heuristica e contar, em cada linha candidata, quantas celulas sao apelido
    conhecido. A linha de titulo tem uma celula de texto livre e pontua zero; a
    de cabecalho pontua quase o numero de colunas. Ganha a de maior pontuacao.

    Devolve o indice da linha (base zero) e o mapa posicao -> coluna canonica.

    Limitacao: planilha com duas tabelas na mesma aba quebra isso, porque o
    segundo cabecalho tambem pontua alto e so o primeiro e considerado.
    """
    limite = min(cfg.linhas_busca_cabecalho, len(bruto))
    minimo_exigido = max(3, len(cfg.validacao["obrigatorios"]))

    melhor_linha = -1
    melhor_mapa: dict[int, str] = {}

    for indice in range(limite):
        mapa = {
            posicao: apelidos[normalizar_cabecalho(valor)]
            for posicao, valor in enumerate(bruto.iloc[indice])
            if not celula_vazia(valor) and normalizar_cabecalho(valor) in apelidos
        }
        if len(mapa) > len(melhor_mapa):
            melhor_linha, melhor_mapa = indice, mapa

    if len(melhor_mapa) < minimo_exigido:
        primeiras = [
            [str(v) for v in bruto.iloc[i] if not celula_vazia(v)][:6]
            for i in range(min(4, len(bruto)))
        ]
        if melhor_mapa:
            pontuacao = (
                f"a melhor pontuacao foi {len(melhor_mapa)} apelido(s) reconhecido(s) "
                f"na linha {melhor_linha + 1}"
            )
        else:
            pontuacao = "nenhuma linha teve um unico apelido reconhecido"
        raise ErroLeitura(
            f"nao achei cabecalho em {arquivo.name}: nas primeiras {limite} linhas "
            f"{pontuacao}, e o minimo e {minimo_exigido}. "
            f"Primeiras linhas do arquivo: {primeiras}. "
            f"Confira a secao 'colunas' do config.yaml"
        )

    return melhor_linha, melhor_mapa


def _e_rodape(valores: dict[str, Any]) -> bool:
    """
    Diz se a linha e somatorio de rodape, e nao uma ordem de producao.

    O criterio e a celula de texto que abre a linha ser um marcador conhecido.
    Nao basta 'nao tem numero de OS': linha com OS vazia e sujeira proposital e
    tem que chegar na validacao para virar ocorrencia no relatorio.
    """
    for valor in valores.values():
        if isinstance(valor, str) and valor.strip().upper() in MARCADORES_RODAPE:
            return True
    return False


def _resumir_linha(valores: dict[str, Any], limite: int = 5) -> str:
    """Resume a linha descartada, para o motivo do descarte ser conferivel."""
    preenchidas = [
        f"{campo}={valor!r}" for campo, valor in valores.items() if not celula_vazia(valor)
    ]
    if not preenchidas:
        return "(todas as celulas vazias)"
    resumo = ", ".join(preenchidas[:limite])
    if len(preenchidas) > limite:
        resumo += f", ... (+{len(preenchidas) - limite})"
    return resumo


def ler_arquivo(
    caminho: Path,
    cfg: Configuracao,
    apelidos: dict[str, str],
    resultado: ResultadoLeitura,
) -> list[dict[str, Any]]:
    """
    Le uma planilha e devolve as linhas de dados, ainda cruas.

    Registra em `resultado` o cabecalho encontrado, as colunas que faltaram, as
    que a planilha tem e o config nao conhece, e cada linha descartada.
    """
    try:
        bruto = pd.read_excel(caminho, sheet_name=0, header=None, dtype=object)
    except (OSError, ValueError) as erro:
        raise ErroLeitura(f"nao foi possivel ler {caminho}: {erro}") from erro

    if bruto.empty:
        raise ErroLeitura(f"{caminho.name} nao tem nenhuma linha")

    linha_cabecalho, posicoes = encontrar_cabecalho(bruto, apelidos, cfg, caminho)
    resultado.cabecalho_por_arquivo[caminho.name] = linha_cabecalho + 1

    encontradas = set(posicoes.values())
    ausentes = [campo for campo in cfg.colunas if campo not in encontradas]

    faltando_obrigatoria = [c for c in cfg.validacao["obrigatorios"] if c in ausentes]
    if faltando_obrigatoria:
        raise ErroLeitura(
            f"{caminho.name}: o cabecalho da linha {linha_cabecalho + 1} nao tem as "
            f"colunas obrigatorias {', '.join(faltando_obrigatoria)}. "
            f"Reconhecidas: {', '.join(sorted(encontradas))}. "
            f"Cabecalho lido: "
            f"{[str(v) for v in bruto.iloc[linha_cabecalho] if not celula_vazia(v)]}"
        )

    if ausentes:
        resultado.colunas_ausentes[caminho.name] = ausentes

    ignoradas = [
        str(valor)
        for posicao, valor in enumerate(bruto.iloc[linha_cabecalho])
        if posicao not in posicoes and not celula_vazia(valor)
    ]
    if ignoradas:
        resultado.colunas_ignoradas[caminho.name] = ignoradas

    linhas: list[dict[str, Any]] = []
    for indice in range(linha_cabecalho + 1, len(bruto)):
        linha_planilha = indice + 1
        valores: dict[str, Any] = {campo: None for campo in cfg.colunas}
        for posicao, campo in posicoes.items():
            valor = bruto.iat[indice, posicao]
            valores[campo] = None if celula_vazia(valor) else valor

        if all(valor is None for valor in valores.values()):
            resultado.descartes.append(
                Descarte(caminho.name, linha_planilha, MOTIVO_LINHA_VAZIA, "")
            )
            continue

        if _e_rodape(valores):
            resultado.descartes.append(
                Descarte(
                    caminho.name, linha_planilha, MOTIVO_RODAPE, _resumir_linha(valores)
                )
            )
            continue

        valores[COLUNA_ARQUIVO] = caminho.name
        valores[COLUNA_LINHA] = linha_planilha
        linhas.append(valores)

    return linhas


def ler_pasta(cfg: Configuracao, entrada: Path | None = None) -> ResultadoLeitura:
    """
    Le todas as planilhas da pasta de entrada, em ordem de nome.

    A ordem e alfabetica para a execucao ser reproduzivel: a mesma pasta produz
    sempre o mesmo DataFrame, na mesma ordem de linhas.
    """
    entrada = cfg.caminho("entrada") if entrada is None else Path(entrada)

    if not entrada.is_dir():
        raise ErroLeitura(f"pasta de entrada nao existe ou nao e pasta: {entrada}")

    # '~$arquivo.xlsx' e o arquivo de trava que o Excel cria com a planilha
    # aberta. Nao e planilha, e le-lo daria erro de formato.
    arquivos = sorted(
        c for c in entrada.glob("*.xlsx") if not c.name.startswith("~$")
    )
    if not arquivos:
        raise ErroLeitura(
            f"nenhuma planilha .xlsx em {entrada}. "
            f"Rode 'python -m src.gerador_dados' para criar as de exemplo"
        )

    colunas = list(cfg.colunas) + [COLUNA_ARQUIVO, COLUNA_LINHA]
    resultado = ResultadoLeitura(dados=pd.DataFrame(columns=colunas, dtype=object))
    apelidos = mapa_apelidos(cfg)

    todas: list[dict[str, Any]] = []
    for caminho in arquivos:
        linhas = ler_arquivo(caminho, cfg, apelidos, resultado)
        resultado.arquivos.append(caminho)
        resultado.linhas_por_arquivo[caminho.name] = len(linhas)
        todas.extend(linhas)

    if todas:
        # dtype=object e obrigatorio. Sem ele o pandas 3 infere dtype de texto
        # numa coluna so de string e troca os None por NaN, e a celula
        # obrigatoria vazia - que e sujeira a ser reportada - chegaria na
        # limpeza como o texto 'nan'. Com object, vazio e sempre None.
        resultado.dados = pd.DataFrame(todas, columns=colunas, dtype=object)

    return resultado


def descrever_resultado(resultado: ResultadoLeitura) -> str:
    """Monta o relatorio legivel da leitura."""
    linhas = ["Arquivos lidos"]
    for caminho in resultado.arquivos:
        nome = caminho.name
        linhas.append(
            f"  {nome:<34} cabecalho na linha {resultado.cabecalho_por_arquivo[nome]:>2}"
            f"   {resultado.linhas_por_arquivo[nome]:>4} linhas de dados"
        )
    linhas.append(f"  {'TOTAL':<34} {'':<22} {len(resultado.dados):>4} linhas")

    if resultado.descartes:
        por_motivo: dict[str, int] = {}
        for descarte in resultado.descartes:
            por_motivo[descarte.motivo] = por_motivo.get(descarte.motivo, 0) + 1

        linhas.append("")
        linhas.append(f"Linhas descartadas ({len(resultado.descartes)})")
        for motivo in sorted(por_motivo):
            linhas.append(f"  {motivo:<34} {por_motivo[motivo]}")
        linhas.append("  detalhe:")
        for descarte in resultado.descartes:
            conteudo = f"  {descarte.conteudo}" if descarte.conteudo else ""
            linhas.append(
                f"    {descarte.arquivo} linha {descarte.linha_planilha}: "
                f"{descarte.motivo}{conteudo}"
            )

    if resultado.colunas_ausentes:
        linhas.append("")
        linhas.append("ATENCAO: colunas do config que a planilha nao tem")
        for arquivo, ausentes in resultado.colunas_ausentes.items():
            linhas.append(f"  {arquivo}: {', '.join(ausentes)}")

    if resultado.colunas_ignoradas:
        linhas.append("")
        linhas.append("ATENCAO: colunas da planilha que o config nao conhece")
        for arquivo, ignoradas in resultado.colunas_ignoradas.items():
            linhas.append(f"  {arquivo}: {', '.join(ignoradas)}")

    linhas.append("")
    linhas.append("Colunas do DataFrame")
    linhas.append(f"  {', '.join(resultado.dados.columns)}")

    return "\n".join(linhas)


def _argumentos(argv: list[str]) -> argparse.Namespace:
    analisador = argparse.ArgumentParser(
        prog="python -m src.leitura",
        description="Le as planilhas da pasta de entrada e informa o que veio de cada uma.",
    )
    analisador.add_argument("--config", default=str(CONFIG_PADRAO), help="caminho do config.yaml")
    analisador.add_argument("--entrada", help="sobrepoe caminhos.entrada")
    analisador.add_argument(
        "--amostra", type=int, default=0, help="imprime as N primeiras linhas lidas"
    )
    return analisador.parse_args(argv)


def main(argv: list[str]) -> int:
    """Entrada de 'python -m src.leitura'."""
    args = _argumentos(argv)

    try:
        cfg = carregar(args.config)
    except ErroConfiguracao as erro:
        print(f"ERRO DE CONFIGURACAO: {erro}", file=sys.stderr)
        return CODIGO_SAIDA_ERRO_CONFIG

    try:
        resultado = ler_pasta(cfg, args.entrada)
    except ErroLeitura as erro:
        print(f"ERRO DE LEITURA: {erro}", file=sys.stderr)
        return 1

    print(descrever_resultado(resultado))

    if args.amostra > 0:
        print("")
        print(f"Primeiras {args.amostra} linhas, como saem da leitura")
        with pd.option_context("display.max_columns", None, "display.width", 250):
            print(resultado.dados.head(args.amostra))

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
