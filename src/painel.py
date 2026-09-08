"""
Painel HTML autocontido, gerado a partir do banco.

Secao 6.1: arquivo unico que abre offline com dois cliques. Os graficos sao
desenhados com matplotlib, convertidos em PNG e embutidos como
'data:image/png;base64,...' dentro do proprio HTML. Sem CDN, sem internet, sem
pasta de imagens ao lado - copiar o arquivo para um pendrive leva o painel
inteiro junto.

Tres cuidados governam o modulo:

1. Nada pintado de verde sem ter sido medido. O indicador que veio None da
   camada de indicadores aparece como 'sem dado', em cinza. Nem verde nem
   vermelho: o que nao foi medido nao passou nem falhou na meta.

2. O periodo incompleto e marcado, nunca escondido. O ultimo mes sofre censura
   a direita (secao 4.4), e no grafico de OTD ele sai com ponto vazado, traco
   pontilhado, faixa sombreada e nota de rodape. Remover o mes maquiaria o
   resultado; deixa-lo sem marca seria mentir por omissao.

3. Todo texto vindo da planilha passa por html.escape antes de entrar na pagina.
   Nome de produto e campo livre digitado no chao de fabrica: um '<' perdido ali
   quebraria a pagina inteira em silencio, e um painel quebrado que ainda abre e
   pior que um que nao abre.

O rodape mostra a propria taxa de erro de ingestao, como manda a secao 6.1. Um
painel que exibe quantas linhas ele perdeu no caminho e um painel em que da para
confiar.
"""

import argparse
import base64
import html
import io
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import matplotlib

# Backend sem tela. O painel precisa rodar em agendador, num servidor que nao
# tem ambiente grafico nenhum; sem esta linha o matplotlib tenta abrir uma
# janela e quebra. Vem antes do import de pyplot de proposito.
matplotlib.use("Agg")

import matplotlib.pyplot as plt  # noqa: E402  (depende do use('Agg') acima)

from src.banco import ErroBanco, conectar
from src.config import (
    CODIGO_SAIDA_ERRO_CONFIG,
    CONFIG_PADRAO,
    Configuracao,
    ErroConfiguracao,
    carregar,
)
from src.indicadores import (
    ErroIndicadores,
    ResultadoIndicadores,
    calcular,
    maquinas_na_meta,
    meta_de,
    na_meta,
    produtividade_na_meta,
)
from src.limpeza import STATUS_ENTREGUE

DPI = 130

# Folga minima entre o topo da barra e a linha de meta, em fracao da altura do
# eixo, para o rotulo caber acima da barra sem encostar no tracejado. Abaixo
# disso o rotulo vai para dentro da barra. O valor cobre um rotulo de ~10pt com
# o deslocamento usado em _rotular_barra.
FOLGA_DO_ROTULO = 0.08


class ErroPainel(Exception):
    """Painel que nao pode ser desenhado ou gravado."""


@dataclass(frozen=True)
class Atraso:
    """Uma ordem entregue depois do prazo."""

    numero_os: str
    cliente: str
    produto: str
    maquina: str
    data_prazo: str
    data_entrega: str
    dias: int


@dataclass(frozen=True)
class Cartao:
    """Um cartao de KPI: o que ele mede, quanto deu e se bateu a meta."""

    titulo: str
    valor: str
    detalhe: str
    meta: str
    estado: bool | None


# ---------------------------------------------------------------------------
# Formatacao
# ---------------------------------------------------------------------------

def _numero(valor: float | None, casas: int = 1, sufixo: str = "") -> str:
    """
    Formata um numero no padrao pt-BR, ou 'sem dado' quando nao ha valor.

    O separador de milhar e ponto e o decimal e virgula, que e como quem le o
    painel escreve numero. A troca em duas etapas passa por um marcador
    temporario porque trocar ',' por '.' e '.' por ',' em sequencia desfaria a
    primeira troca.
    """
    if valor is None:
        return "sem dado"
    texto = f"{valor:,.{casas}f}"
    return texto.replace(",", "\x00").replace(".", ",").replace("\x00", ".") + sufixo


def _dias(quantidade: int) -> str:
    return f"{quantidade} dia" if quantidade == 1 else f"{quantidade} dias"


# ---------------------------------------------------------------------------
# Graficos
# ---------------------------------------------------------------------------

def _nova_figura(cfg: Configuracao, largura: float = 9.0, altura: float = 3.6):
    """Cria figura e eixo ja no tema escuro do painel."""
    cores = cfg.painel["cores"]
    figura, eixo = plt.subplots(figsize=(largura, altura))
    figura.patch.set_facecolor(cores["cartao"])
    eixo.set_facecolor(cores["cartao"])
    for lado in ("top", "right"):
        eixo.spines[lado].set_visible(False)
    for lado in ("left", "bottom"):
        eixo.spines[lado].set_color(cores["texto_fraco"])
    eixo.tick_params(colors=cores["texto_fraco"], labelsize=9)
    eixo.set_axisbelow(True)
    return figura, eixo


def _para_data_uri(figura) -> str:
    """
    Converte a figura em PNG embutido, e fecha a figura.

    Fechar e obrigatorio: o matplotlib guarda toda figura aberta numa lista
    global, e gerar o painel varias vezes no mesmo processo - como o Bloco 10
    vai fazer - vazaria memoria e emitiria aviso a cada vigesima figura.
    """
    buffer = io.BytesIO()
    figura.savefig(
        buffer,
        format="png",
        dpi=DPI,
        bbox_inches="tight",
        facecolor=figura.get_facecolor(),
    )
    plt.close(figura)
    codificado = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/png;base64,{codificado}"


def _grafico_sem_dado(cfg: Configuracao, mensagem: str) -> str:
    """
    Placeholder para o corte que nao tem nada a mostrar.

    Um espaco em branco no lugar do grafico faria parecer defeito de
    carregamento. A mensagem diz que nao ha dado, que e diferente de zero.
    """
    cores = cfg.painel["cores"]
    figura, eixo = _nova_figura(cfg, altura=2.2)
    eixo.axis("off")
    eixo.text(
        0.5, 0.5, mensagem,
        ha="center", va="center", color=cores["texto_fraco"], fontsize=11,
    )
    return _para_data_uri(figura)


def rotulo_acima_colide(altura: float, meta: float | None, intervalo: float) -> bool:
    """
    Diz se o rotulo escrito acima da barra encostaria na linha de meta.

    So o caso 'meta logo ACIMA do topo da barra' e problema: o rotulo sobe
    direto no tracejado. Meta abaixo do topo nao atrapalha - o rotulo sobe e o
    tracejado fica para tras.

    Funcao separada do desenho para ter teste proprio: a regra e o que erra, e
    ela erra justamente na barra que mais interessa, a que esta no limite.
    """
    if meta is None or intervalo <= 0:
        return False
    return 0 <= (meta - altura) < FOLGA_DO_ROTULO * intervalo


def _rotular_barra(eixo, barra, texto: str, cfg: Configuracao, meta: float | None) -> None:
    """
    Escreve o valor de uma barra vertical, acima dela ou dentro.

    Acima e o padrao, porque le melhor. Quando o rotulo acima encostaria na
    linha de meta, ele vai para dentro da barra, escrito na cor do fundo.
    """
    cores = cfg.painel["cores"]
    altura = barra.get_height()
    inicio, fim = eixo.get_ylim()

    if rotulo_acima_colide(altura, meta, fim - inicio):
        eixo.annotate(
            texto,
            xy=(barra.get_x() + barra.get_width() / 2, altura),
            xytext=(0, -14), textcoords="offset points",
            ha="center", va="top", color=cores["fundo"], fontsize=9.5, weight="bold",
        )
    else:
        eixo.annotate(
            texto,
            xy=(barra.get_x() + barra.get_width() / 2, altura),
            xytext=(0, 5), textcoords="offset points",
            ha="center", va="bottom", color=cores["texto"], fontsize=9.5,
        )


def _cor_da_meta(cfg: Configuracao, dentro: bool | None) -> str:
    """Verde quando bate a meta, vermelho quando nao, cinza quando sem dado."""
    cores = cfg.painel["cores"]
    if dentro is None:
        return cores["texto_fraco"]
    return cores["verde"] if dentro else cores["vermelho"]


def grafico_produtividade(resultado: ResultadoIndicadores, cfg: Configuracao) -> str:
    """
    Barras de folhas boas por hora, uma por maquina.

    A meta entra como um traco sobre cada barra, e nao como uma linha
    horizontal unica: a meta e por maquina, e uma linha so daria a entender que
    da para comparar uma plana com uma digital nesta escala. Nao da.
    """
    cores = cfg.painel["cores"]
    medidas = {
        nome: ind
        for nome, ind in resultado.por_maquina.items()
        if ind.produtividade is not None
    }
    if not medidas:
        return _grafico_sem_dado(cfg, "Nenhuma máquina com tempo de produção no período")

    figura, eixo = _nova_figura(cfg)
    nomes = list(medidas)
    valores = [medidas[n].produtividade for n in nomes]
    cor = [
        _cor_da_meta(cfg, produtividade_na_meta(n, medidas[n], cfg)) for n in nomes
    ]

    barras = eixo.bar(nomes, valores, color=cor, width=0.55)
    for posicao, nome in enumerate(nomes):
        meta = cfg.metas_produtividade.get(nome)
        if meta is None:
            continue
        eixo.hlines(
            meta, posicao - 0.34, posicao + 0.34,
            color=cores["texto"], linestyles="dashed", linewidth=1.4,
        )

    eixo.set_ylabel("folhas boas / hora", color=cores["texto_fraco"], fontsize=9)
    eixo.grid(axis="y", color=cores["texto_fraco"], alpha=0.15, linewidth=0.8)
    eixo.margins(y=0.18)

    # Rotular por ultimo: _rotular_barra decide onde escrever a partir da altura
    # do eixo, e ela so e a definitiva depois de margins().
    for barra, nome, valor in zip(barras, nomes, valores):
        _rotular_barra(
            eixo, barra, _numero(valor, 0), cfg, cfg.metas_produtividade.get(nome)
        )
    return _para_data_uri(figura)


def grafico_refugo(resultado: ResultadoIndicadores, cfg: Configuracao) -> str:
    """Barras de percentual de refugo por maquina, com a linha da meta global."""
    cores = cfg.painel["cores"]
    meta = meta_de("refugo_pct", cfg)
    medidas = {
        nome: ind
        for nome, ind in resultado.por_maquina.items()
        if ind.refugo_pct is not None
    }
    if not medidas:
        return _grafico_sem_dado(cfg, "Nenhuma máquina com material processado no período")

    figura, eixo = _nova_figura(cfg)
    nomes = list(medidas)
    valores = [medidas[n].refugo_pct for n in nomes]
    cor = [_cor_da_meta(cfg, na_meta("refugo_pct", v, meta)) for v in valores]

    barras = eixo.bar(nomes, valores, color=cor, width=0.55)
    eixo.axhline(
        meta, color=cores["texto"], linestyle="dashed", linewidth=1.4,
        label=f"meta: máximo {_numero(meta, 1, '%')}",
    )
    eixo.set_ylabel("refugo (%)", color=cores["texto_fraco"], fontsize=9)
    eixo.grid(axis="y", color=cores["texto_fraco"], alpha=0.15, linewidth=0.8)
    eixo.margins(y=0.18)

    # Depois de margins(), pelo mesmo motivo do grafico de produtividade. Aqui a
    # meta e a mesma para todas as barras, e e ela que a Komori encosta.
    for barra, valor in zip(barras, valores):
        _rotular_barra(eixo, barra, _numero(valor, 2, "%"), cfg, meta)

    legenda = eixo.legend(
        facecolor=cores["cartao"], edgecolor=cores["texto_fraco"], fontsize=9
    )
    for texto in legenda.get_texts():
        texto.set_color(cores["texto_fraco"])
    return _para_data_uri(figura)


def grafico_otd_mensal(resultado: ResultadoIndicadores, cfg: Configuracao) -> str:
    """
    Evolucao mensal do OTD, por coorte de entrada.

    O periodo incompleto recebe tres marcas ao mesmo tempo - faixa sombreada,
    traco pontilhado e ponto vazado - porque uma so passa despercebida em print
    de portfolio, que e como este painel vai ser visto na maior parte das vezes.
    A nota de rodape no HTML explica por que o valor dele e otimista.
    """
    cores = cfg.painel["cores"]
    meta = meta_de("otd_pct", cfg)
    medidos = {
        mes: ind.otd_pct
        for mes, ind in resultado.por_mes.items()
        if ind.otd_pct is not None
    }
    if not medidos:
        return _grafico_sem_dado(cfg, "Nenhuma ordem entregue com prazo no período")

    figura, eixo = _nova_figura(cfg)
    meses = list(medidos)
    valores = [medidos[m] for m in meses]
    incompleto = resultado.mes_incompleto if resultado.mes_incompleto in medidos else None
    # Indice do mes censurado. A linha cheia PARA antes dele: o trecho que chega
    # nele e desenhado a parte, pontilhado, porque aquele valor ainda vai mudar.
    # Desenhar a linha cheia ate o fim e pontilhar por cima nao funciona - a
    # cheia cobre a pontilhada e a marcacao some sem ninguem perceber.
    corte = meses.index(incompleto) if incompleto else len(meses)

    eixo.plot(
        meses[:corte], valores[:corte],
        color=cores["verde"], linewidth=2.2, marker="o", markersize=6,
    )
    if incompleto:
        if corte:
            eixo.plot(
                meses[corte - 1 : corte + 1], valores[corte - 1 : corte + 1],
                color=cores["verde"], linewidth=2.2, linestyle=(0, (2, 2)),
            )
        # Ponto vazado: o miolo com a cor do fundo do cartao.
        eixo.plot(
            [incompleto], [medidos[incompleto]],
            marker="o", markersize=9, markeredgewidth=2.2, linestyle="none",
            markeredgecolor=cores["verde"], markerfacecolor=cores["cartao"],
        )
        eixo.axvspan(
            corte - 0.5, corte + 0.5, color=cores["texto_fraco"], alpha=0.16
        )
        eixo.annotate(
            "período incompleto",
            xy=(corte, medidos[incompleto]),
            xytext=(0, 26), textcoords="offset points",
            ha="center", color=cores["texto_fraco"], fontsize=8.5,
        )

    eixo.axhline(
        meta, color=cores["texto"], linestyle="dashed", linewidth=1.4,
        label=f"meta: mínimo {_numero(meta, 0, '%')}",
    )
    # Deslocamento em pontos, e nao em unidade do eixo: o rotulo precisa ficar
    # a mesma distancia do marcador independente da escala do OTD no periodo.
    for mes, valor in zip(meses, valores):
        eixo.annotate(
            _numero(valor, 1),
            xy=(mes, valor), xytext=(0, 11), textcoords="offset points",
            ha="center", color=cores["texto"], fontsize=8.5,
        )

    eixo.set_ylabel("OTD (%)", color=cores["texto_fraco"], fontsize=9)
    eixo.grid(axis="y", color=cores["texto_fraco"], alpha=0.15, linewidth=0.8)
    eixo.margins(y=0.22, x=0.05)
    legenda = eixo.legend(
        facecolor=cores["cartao"], edgecolor=cores["texto_fraco"], fontsize=9, loc="lower left"
    )
    for texto in legenda.get_texts():
        texto.set_color(cores["texto_fraco"])
    return _para_data_uri(figura)


def grafico_setup(resultado: ResultadoIndicadores, cfg: Configuracao) -> str:
    """Barras horizontais de tempo medio de acerto, com a linha do teto global."""
    cores = cfg.painel["cores"]
    meta = meta_de("setup_medio_min", cfg)
    medidas = {
        nome: ind
        for nome, ind in resultado.por_maquina.items()
        if ind.setup_medio_min is not None
    }
    if not medidas:
        return _grafico_sem_dado(cfg, "Nenhuma máquina com tempo de acerto no período")

    figura, eixo = _nova_figura(cfg, altura=3.0)
    # Invertido para a primeira maquina ficar no topo, como se le uma lista.
    nomes = list(reversed(list(medidas)))
    valores = [medidas[n].setup_medio_min for n in nomes]
    cor = [_cor_da_meta(cfg, na_meta("setup_medio_min", v, meta)) for v in valores]

    barras = eixo.barh(nomes, valores, color=cor, height=0.55)
    eixo.axvline(
        meta, color=cores["texto"], linestyle="dashed", linewidth=1.4,
        label=f"meta: máximo {_numero(meta, 0)} min",
    )
    for barra, valor in zip(barras, valores):
        eixo.text(
            barra.get_width(), barra.get_y() + barra.get_height() / 2,
            f"  {_numero(valor, 1)} min",
            va="center", color=cores["texto"], fontsize=9,
        )

    eixo.set_xlabel("minutos por job", color=cores["texto_fraco"], fontsize=9)
    eixo.grid(axis="x", color=cores["texto_fraco"], alpha=0.15, linewidth=0.8)
    eixo.margins(x=0.16)
    legenda = eixo.legend(
        facecolor=cores["cartao"], edgecolor=cores["texto_fraco"], fontsize=9, loc="lower right"
    )
    for texto in legenda.get_texts():
        texto.set_color(cores["texto_fraco"])
    return _para_data_uri(figura)


# ---------------------------------------------------------------------------
# Consultas proprias do painel
# ---------------------------------------------------------------------------

def ordens_atrasadas(
    conexao: sqlite3.Connection,
    resultado: ResultadoIndicadores,
    limite: int | None,
) -> list[Atraso]:
    """
    As ordens entregues fora do prazo, da mais atrasada para a menos.

    Mesmo recorte dos indicadores: filtra pela data de ENTRADA, para a tabela
    falar do mesmo conjunto de ordens que os graficos acima dela.

    'limite' None traz todas. O painel pede as dez primeiras porque e para
    olhar de longe; o relatorio Excel pede todas porque e para filtrar em cima.
    """
    linhas = conexao.execute(
        """
        SELECT
            ordem_producao.numero_os,
            ordem_producao.cliente,
            ordem_producao.produto,
            maquina.nome_canonico AS maquina,
            ordem_producao.data_prazo,
            ordem_producao.data_entrega,
            CAST(julianday(ordem_producao.data_entrega)
                 - julianday(ordem_producao.data_prazo) AS INTEGER) AS dias
        FROM ordem_producao
        JOIN maquina ON maquina.id = ordem_producao.maquina_id
        WHERE ordem_producao.status = :entregue
          AND ordem_producao.data_entrega IS NOT NULL
          AND ordem_producao.data_prazo IS NOT NULL
          AND ordem_producao.data_entrega > ordem_producao.data_prazo
          AND ordem_producao.data_entrada BETWEEN :de AND :ate
        ORDER BY dias DESC, ordem_producao.numero_os
        LIMIT :limite
        """,
        {
            "entregue": STATUS_ENTREGUE,
            "de": resultado.de,
            "ate": resultado.ate,
            # LIMIT -1 no SQLite significa sem limite. E o valor documentado
            # para o caso, e evita montar duas versoes da consulta.
            "limite": -1 if limite is None else limite,
        },
    ).fetchall()

    return [
        Atraso(
            numero_os=linha["numero_os"],
            cliente=linha["cliente"] or "",
            produto=linha["produto"] or "",
            maquina=linha["maquina"],
            data_prazo=linha["data_prazo"],
            data_entrega=linha["data_entrega"],
            dias=linha["dias"],
        )
        for linha in linhas
    ]


def ultima_importacao(conexao: sqlite3.Connection) -> sqlite3.Row | None:
    """
    O registro da importacao mais recente, para o rodape de qualidade.

    Devolve None quando o banco foi populado sem passar por 'src.banco'. O
    rodape diz isso em vez de mostrar zeros, que pareceriam uma importacao
    perfeita.
    """
    return conexao.execute(
        "SELECT * FROM execucao_importacao ORDER BY id DESC LIMIT 1"
    ).fetchone()


# ---------------------------------------------------------------------------
# Cartoes de KPI
# ---------------------------------------------------------------------------

def montar_cartoes(resultado: ResultadoIndicadores, cfg: Configuracao) -> list[Cartao]:
    """Os cinco cartoes do topo, com valor, meta e estado."""
    total = resultado.total
    dentro, medidas, pct_maquinas = maquinas_na_meta(resultado, cfg)
    meta_maquinas = cfg.metas["maquinas_na_meta_minimo_pct"]

    # O cartao de produtividade nao mostra media de folhas/hora: media entre uma
    # plana e uma digital nao tem meta que signifique nada. Mostra quantas
    # maquinas batem a propria meta.
    cartoes = [
        Cartao(
            # O titulo diz o que o numero e. 'Produtividade' com o valor '1 de 4'
            # obrigava a ler duas linhas abaixo para descobrir que a contagem e
            # de maquinas, e nao de folhas por hora.
            titulo="Máquinas na meta",
            valor=f"{dentro} de {medidas}" if medidas else "sem dado",
            detalhe="batem a própria meta de folhas por hora",
            meta=f"mínimo {_numero(meta_maquinas, 0, '%')} das máquinas",
            estado=na_meta("maquinas_na_meta_pct", pct_maquinas, meta_maquinas),
        )
    ]

    for titulo, indicador, casas, sufixo, detalhe in (
        ("Refugo", "refugo_pct", 2, "%", "do material que passou pela máquina"),
        ("Tempo de acerto", "setup_medio_min", 1, " min", "média por job"),
        ("Entrega no prazo", "otd_pct", 1, "%", "das ordens entregues com prazo"),
        (
            "Aproveitamento",
            "aproveitamento_pct",
            1,
            "%",
            "do tempo de máquina foi produção",
        ),
    ):
        valor = getattr(total, indicador)
        meta = meta_de(indicador, cfg)
        rotulo = "máximo" if indicador in ("refugo_pct", "setup_medio_min") else "mínimo"
        casas_meta = 2 if indicador == "refugo_pct" else 0
        cartoes.append(
            Cartao(
                titulo=titulo,
                valor=_numero(valor, casas, sufixo),
                detalhe=detalhe,
                meta=f"{rotulo} {_numero(meta, casas_meta, sufixo)}",
                estado=na_meta(indicador, valor, meta),
            )
        )

    return cartoes


# ---------------------------------------------------------------------------
# Montagem do HTML
# ---------------------------------------------------------------------------

ESTILO = """
* { box-sizing: border-box; }
body {
  margin: 0;
  padding: 32px 20px 56px;
  background: {fundo};
  color: {texto};
  font-family: "Segoe UI", system-ui, -apple-system, Arial, sans-serif;
  font-size: 15px;
  line-height: 1.55;
}
.pagina { max-width: 1080px; margin: 0 auto; }
header { border-bottom: 1px solid {borda}; padding-bottom: 18px; margin-bottom: 26px; }
h1 { margin: 0 0 6px; font-size: 26px; letter-spacing: -0.01em; }
.subtitulo { color: {texto_fraco}; font-size: 14px; margin: 0; }
h2 { font-size: 15px; text-transform: uppercase; letter-spacing: 0.08em;
     color: {texto_fraco}; font-weight: 600; margin: 34px 0 14px; }
.kpis { display: grid; gap: 14px; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); }
.cartao { background: {cartao}; border: 1px solid {borda}; border-radius: 10px;
          padding: 16px 18px; border-top: 3px solid {texto_fraco}; }
.cartao.dentro { border-top-color: {verde}; }
.cartao.fora   { border-top-color: {vermelho}; }
.cartao .titulo { color: {texto_fraco}; font-size: 12.5px; text-transform: uppercase;
                  letter-spacing: 0.06em; margin-bottom: 8px; }
.cartao .valor { font-size: 30px; font-weight: 650; line-height: 1.1; }
.cartao.dentro .valor { color: {verde}; }
.cartao.fora   .valor { color: {vermelho}; }
.cartao.sem    .valor { color: {texto_fraco}; font-size: 22px; }
.cartao .detalhe { color: {texto_fraco}; font-size: 12.5px; margin-top: 6px; }
.cartao .meta { color: {texto_fraco}; font-size: 12px; margin-top: 10px;
                padding-top: 9px; border-top: 1px solid {borda}; }
.grafico { background: {cartao}; border: 1px solid {borda}; border-radius: 10px;
           padding: 14px; margin-bottom: 8px; }
.grafico img { width: 100%; height: auto; display: block; }
.nota { color: {texto_fraco}; font-size: 12.5px; margin: 0 0 22px; }
table { width: 100%; border-collapse: collapse; background: {cartao};
        border: 1px solid {borda}; border-radius: 10px; overflow: hidden; font-size: 13.5px; }
th { text-align: left; color: {texto_fraco}; font-weight: 600; font-size: 12px;
     text-transform: uppercase; letter-spacing: 0.05em;
     padding: 11px 14px; border-bottom: 1px solid {borda}; }
td { padding: 10px 14px; border-bottom: 1px solid {borda}; }
tr:last-child td { border-bottom: none; }
td.numero, th.numero { text-align: right; white-space: nowrap; }
td.atraso { color: {vermelho}; font-weight: 600; }
.vazio { color: {texto_fraco}; padding: 18px 0; }
footer { margin-top: 40px; border-top: 1px solid {borda}; padding-top: 20px; }
.qualidade { display: grid; gap: 12px;
             grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); }
.qualidade div { background: {cartao}; border: 1px solid {borda};
                 border-radius: 8px; padding: 12px 14px; }
.qualidade .n { font-size: 20px; font-weight: 650; }
.qualidade .r { color: {texto_fraco}; font-size: 12px; text-transform: uppercase;
                letter-spacing: 0.05em; }
.rodape { color: {texto_fraco}; font-size: 12px; margin-top: 18px; }
"""


def _estilo(cfg: Configuracao) -> str:
    """Injeta as cores do config.yaml no CSS."""
    cores = dict(cfg.painel["cores"])
    # Borda nao esta no config porque nao e uma cor de identidade, e o contorno
    # discreto dos cartoes. Sai da cor do cartao com transparencia.
    cores["borda"] = "#1b2a30"
    estilo = ESTILO
    for nome, valor in cores.items():
        estilo = estilo.replace("{" + nome + "}", valor)
    return estilo


def _classe(estado: bool | None) -> str:
    if estado is None:
        return "sem"
    return "dentro" if estado else "fora"


def _html_cartoes(cartoes: list[Cartao]) -> str:
    partes = []
    for cartao in cartoes:
        partes.append(
            f'<div class="cartao {_classe(cartao.estado)}">'
            f'<div class="titulo">{html.escape(cartao.titulo)}</div>'
            f'<div class="valor">{html.escape(cartao.valor)}</div>'
            f'<div class="detalhe">{html.escape(cartao.detalhe)}</div>'
            f'<div class="meta">{html.escape(cartao.meta)}</div>'
            f"</div>"
        )
    return f'<section class="kpis">{"".join(partes)}</section>'


def _html_grafico(titulo: str, imagem: str, nota: str = "") -> str:
    rodape = f'<p class="nota">{nota}</p>' if nota else '<p class="nota"></p>'
    return (
        f"<h2>{html.escape(titulo)}</h2>"
        f'<div class="grafico"><img alt="{html.escape(titulo)}" src="{imagem}"></div>'
        f"{rodape}"
    )


def _html_atrasos(atrasos: list[Atraso], limite: int) -> str:
    if not atrasos:
        return (
            f"<h2>As {limite} ordens mais atrasadas</h2>"
            f'<p class="vazio">Nenhuma ordem entregue fora do prazo no período.</p>'
        )

    corpo = []
    for atraso in atrasos:
        corpo.append(
            "<tr>"
            f"<td>{html.escape(atraso.numero_os)}</td>"
            f"<td>{html.escape(atraso.cliente)}</td>"
            f"<td>{html.escape(atraso.produto)}</td>"
            f"<td>{html.escape(atraso.maquina)}</td>"
            f'<td class="numero">{html.escape(atraso.data_prazo)}</td>'
            f'<td class="numero">{html.escape(atraso.data_entrega)}</td>'
            f'<td class="numero atraso">{html.escape(_dias(atraso.dias))}</td>'
            "</tr>"
        )
    return (
        f"<h2>As {len(atrasos)} ordens mais atrasadas</h2>"
        "<table><thead><tr>"
        "<th>OS</th><th>Cliente</th><th>Produto</th><th>Máquina</th>"
        '<th class="numero">Prazo</th><th class="numero">Entrega</th>'
        '<th class="numero">Atraso</th>'
        "</tr></thead><tbody>"
        f"{''.join(corpo)}"
        "</tbody></table>"
    )


def _html_qualidade(execucao: sqlite3.Row | None) -> str:
    """
    Rodape com a taxa de erro de ingestao.

    Deliberado, secao 6.1: um painel que mostra quantas linhas ele perdeu no
    caminho e um painel em que da para confiar.
    """
    if execucao is None:
        return (
            "<h2>Qualidade dos dados</h2>"
            '<p class="vazio">Não há registro de importação neste banco. '
            "Os números acima não podem ser acompanhados de taxa de ingestão.</p>"
        )

    lidas = execucao["linhas_lidas"]
    rejeitadas = execucao["linhas_rejeitadas"]
    taxa = rejeitadas / lidas * 100 if lidas else None

    campos = (
        ("linhas lidas", f"{lidas:,}".replace(",", ".")),
        ("gravadas", f"{execucao['linhas_gravadas']:,}".replace(",", ".")),
        ("corrigidas", f"{execucao['linhas_corrigidas']:,}".replace(",", ".")),
        ("rejeitadas", f"{rejeitadas:,}".replace(",", ".")),
        ("taxa de rejeição", _numero(taxa, 2, "%")),
    )
    blocos = "".join(
        f'<div><div class="r">{html.escape(rotulo)}</div>'
        f'<div class="n">{html.escape(valor)}</div></div>'
        for rotulo, valor in campos
    )
    return (
        "<h2>Qualidade dos dados</h2>"
        f'<div class="qualidade">{blocos}</div>'
        f'<p class="rodape">Importação de {html.escape(execucao["executado_em"])}, '
        f'sobre {execucao["arquivos_lidos"]} arquivo(s). '
        f"A linha corrigida entrou no painel; a rejeitada não entrou, e o motivo "
        f"de cada uma está em saida/relatorio_qualidade.csv.</p>"
    )


def montar_html(
    resultado: ResultadoIndicadores,
    cfg: Configuracao,
    graficos: dict[str, str],
    atrasos: list[Atraso],
    execucao: sqlite3.Row | None,
    gerado_em: str,
) -> str:
    """Monta a pagina inteira, ja com as imagens embutidas."""
    titulo = cfg.painel["titulo"]
    nota_incompleto = ""
    if resultado.mes_incompleto:
        nota_incompleto = (
            f"O ponto vazado em {resultado.mes_incompleto} marca um período "
            f"incompleto: a exportação foi cortada dentro dele. O OTD desse mês é "
            f"otimista — a ordem que atrasou ainda não foi entregue, então ela não "
            f"entra no denominador. O valor tende a cair quando o mês fechar."
        )

    ordens = f"{resultado.total.ordens:,}".replace(",", ".")
    subtitulo = (
        f"Período por coorte de entrada: {resultado.de} a {resultado.ate}"
        f" · {ordens} ordens · gerado em {gerado_em}"
    )

    return (
        "<!DOCTYPE html>"
        '<html lang="pt-BR"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{html.escape(titulo)}</title>"
        f"<style>{_estilo(cfg)}</style></head><body>"
        '<div class="pagina">'
        "<header>"
        f"<h1>{html.escape(titulo)}</h1>"
        f'<p class="subtitulo">{html.escape(subtitulo)}</p>'
        "</header>"
        f"{_html_cartoes(montar_cartoes(resultado, cfg))}"
        f"{_html_grafico('Produtividade por máquina', graficos['produtividade'], 'O traço sobre cada barra é a meta daquela máquina. Folhas por hora não é comparável entre equipamentos de portes diferentes — cada barra só se compara consigo mesma ao longo do tempo.')}"
        f"{_html_grafico('Refugo por máquina', graficos['refugo'])}"
        f"{_html_grafico('Entrega no prazo, por mês de entrada', graficos['otd'], nota_incompleto)}"
        f"{_html_grafico('Tempo médio de acerto por máquina', graficos['setup'])}"
        f"{_html_atrasos(atrasos, cfg.painel['top_atrasos'])}"
        f"<footer>{_html_qualidade(execucao)}</footer>"
        "</div></body></html>"
    )


# ---------------------------------------------------------------------------
# Orquestracao
# ---------------------------------------------------------------------------

def gerar(
    cfg: Configuracao,
    banco: Path | None = None,
    de: str | None = None,
    ate: str | None = None,
    destino: Path | None = None,
) -> tuple[Path, int]:
    """
    Gera o painel e devolve o caminho gravado e o tamanho em bytes.

    O tamanho e devolvido porque ele e a unica pista de que os PNG entraram: um
    HTML de dois kilobytes significa grafico vazio, e isso passaria despercebido
    sem alguem abrir o arquivo.
    """
    caminho_banco = cfg.caminho("banco") if banco is None else Path(banco)
    if not caminho_banco.is_file():
        raise ErroPainel(
            f"banco nao encontrado em {caminho_banco}. "
            f"Rode 'python -m src.banco' antes"
        )

    destino = cfg.arquivo_painel() if destino is None else Path(destino)
    conexao = conectar(caminho_banco)
    try:
        resultado = calcular(conexao, de, ate)
        graficos = {
            "produtividade": grafico_produtividade(resultado, cfg),
            "refugo": grafico_refugo(resultado, cfg),
            "otd": grafico_otd_mensal(resultado, cfg),
            "setup": grafico_setup(resultado, cfg),
        }
        atrasos = ordens_atrasadas(conexao, resultado, cfg.painel["top_atrasos"])
        execucao = ultima_importacao(conexao)
    finally:
        conexao.close()

    pagina = montar_html(
        resultado,
        cfg,
        graficos,
        atrasos,
        execucao,
        gerado_em=datetime.now().strftime("%d/%m/%Y %H:%M"),
    )

    try:
        destino.parent.mkdir(parents=True, exist_ok=True)
        destino.write_text(pagina, encoding="utf-8")
    except OSError as erro:
        raise ErroPainel(f"nao foi possivel gravar {destino}: {erro}") from erro

    return destino, len(pagina.encode("utf-8"))


def _argumentos(argv: list[str]) -> argparse.Namespace:
    analisador = argparse.ArgumentParser(
        prog="python -m src.painel",
        description="Gera o painel HTML autocontido a partir do banco.",
    )
    analisador.add_argument(
        "--config", default=str(CONFIG_PADRAO), help="caminho do config.yaml"
    )
    analisador.add_argument("--banco", help="sobrepoe caminhos.banco")
    analisador.add_argument("--saida", help="sobrepoe o arquivo de destino")
    analisador.add_argument("--de", help="inicio do periodo, AAAA-MM-DD")
    analisador.add_argument("--ate", help="fim do periodo, AAAA-MM-DD")
    return analisador.parse_args(argv)


def main(argv: list[str]) -> int:
    """Entrada de 'python -m src.painel'."""
    args = _argumentos(argv)

    try:
        cfg = carregar(args.config)
    except ErroConfiguracao as erro:
        print(f"ERRO DE CONFIGURACAO: {erro}", file=sys.stderr)
        return CODIGO_SAIDA_ERRO_CONFIG

    try:
        destino, tamanho = gerar(
            cfg, banco=args.banco, de=args.de, ate=args.ate, destino=args.saida
        )
    except (ErroPainel, ErroBanco, ErroIndicadores) as erro:
        print(f"ERRO DE PAINEL: {erro}", file=sys.stderr)
        return 1

    print(f"Painel gravado em {destino}")
    print(f"Tamanho: {tamanho / 1024:.0f} KB, autocontido - abre sem internet")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
