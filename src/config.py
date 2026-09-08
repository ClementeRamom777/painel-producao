"""
Carregamento e validacao do config.yaml.

Decisao 1 da especificacao: nome de coluna, sinonimo de maquina, limite de
validacao e meta de KPI ficam fora do codigo. Este modulo e a unica porta de
entrada dessa configuracao - nenhum outro modulo abre o YAML.

A validacao aqui e proposital e agressiva: e melhor o programa recusar a
subir com uma mensagem que nomeia a chave e o valor errado do que quebrar
tres modulos adiante com um TypeError sem contexto.
"""

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

# Raiz do projeto: a pasta que contem o pacote src/.
RAIZ_PROJETO = Path(__file__).resolve().parent.parent
CONFIG_PADRAO = RAIZ_PROJETO / "config.yaml"

# Codigo de saida reservado para erro de configuracao (secao 8 da especificacao).
CODIGO_SAIDA_ERRO_CONFIG = 2

NIVEIS_LOG_ACEITOS = ("DEBUG", "INFO", "WARNING", "ERROR")

# Chaves que precisam existir, com os tipos aceitos para cada uma.
# A ordem importa: o pai vem sempre antes dos filhos, para que um erro no pai
# suprima os erros redundantes dos filhos.
CHAVES_OBRIGATORIAS: tuple[tuple[str, tuple[type, ...]], ...] = (
    ("caminhos", (dict,)),
    ("caminhos.entrada", (str,)),
    ("caminhos.banco", (str,)),
    ("caminhos.saida", (str,)),
    ("caminhos.logs", (str,)),

    ("log", (dict,)),
    ("log.nivel", (str,)),
    ("log.arquivo", (str,)),

    ("gerador", (dict,)),
    ("gerador.semente", (int,)),
    ("gerador.arquivos", (int,)),
    ("gerador.meses", (int,)),
    ("gerador.ordens_por_mes", (int,)),
    ("gerador.taxa_sujeira", (int, float)),
    ("gerador.clientes", (list,)),
    ("gerador.produtos", (list,)),
    ("gerador.operadores", (list,)),

    # A tiragem depende do produto, nao da maquina: e o pedido do cliente que
    # define o tamanho da tiragem; a maquina so determina em quanto tempo sai.
    ("gerador.tiragem_por_produto", (dict,)),
    ("gerador.maquinas_digitais", (list,)),
    ("gerador.tiragem_maxima_digital", (int,)),
    ("gerador.peso_digital_tiragem_curta", (int,)),
    ("gerador.setup_por_maquina", (dict,)),
    ("gerador.refugo_por_maquina", (dict,)),

    ("gerador.atrasos", (dict,)),
    ("gerador.atrasos.otd_alvo_pct", (int, float)),
    ("gerador.atrasos.dias_atraso", (list,)),
    ("gerador.atrasos.concentracao", (dict,)),
    ("gerador.atrasos.concentracao.maquina", (str,)),
    ("gerador.atrasos.concentracao.mes", (str,)),
    ("gerador.atrasos.concentracao.otd_pct", (int, float)),

    ("gerador.fechamento_tiragem", (dict,)),
    ("gerador.fechamento_tiragem.sobra_maxima_pct", (int, float)),
    ("gerador.fechamento_tiragem.prob_abaixo_do_pedido", (int, float)),
    ("gerador.fechamento_tiragem.falta_maxima_pct", (int, float)),
    ("gerador.fechamento_tiragem.prob_exato", (int, float)),

    ("gerador.status", (dict,)),
    ("gerador.status.dias_janela_em_producao", (int,)),
    ("gerador.status.prob_em_producao", (int, float)),
    ("gerador.status.prob_cancelada", (int, float)),

    ("colunas", (dict,)),
    ("colunas.numero_os", (list,)),
    ("colunas.cliente", (list,)),
    ("colunas.produto", (list,)),
    ("colunas.maquina", (list,)),
    ("colunas.operador", (list,)),
    ("colunas.turno", (list,)),
    ("colunas.data_entrada", (list,)),
    ("colunas.data_prazo", (list,)),
    ("colunas.data_entrega", (list,)),
    ("colunas.status", (list,)),
    ("colunas.tiragem_prevista", (list,)),
    ("colunas.tiragem_boa", (list,)),
    ("colunas.refugo", (list,)),
    ("colunas.tempo_setup_min", (list,)),
    ("colunas.tempo_producao_min", (list,)),

    ("linhas_busca_cabecalho", (int,)),
    ("marcadores_rodape", (list,)),

    ("maquinas", (dict,)),
    ("turnos", (dict,)),
    ("status", (dict,)),

    ("validacao", (dict,)),
    ("validacao.obrigatorios", (list,)),
    ("validacao.refugo_maximo_sobre_tiragem", (int, float)),
    ("validacao.tempo_maximo_minutos", (int, float)),
    ("validacao.taxa_rejeicao_limite", (int, float)),

    ("metas", (dict,)),
    ("metas.refugo_maximo_pct", (int, float)),
    ("metas.otd_minimo_pct", (int, float)),
    ("metas.setup_maximo_min", (int, float)),
    ("metas.aproveitamento_minimo_pct", (int, float)),
    # O cartao global de produtividade nao mostra media de folhas/hora - media
    # entre guilhotina e digital nao tem meta que signifique nada. Mostra
    # "X de N maquinas na meta", e este e o percentual que pinta o cartao.
    ("metas.maquinas_na_meta_minimo_pct", (int, float)),

    # Meta de produtividade e por maquina, nao global: folha boa por hora nao
    # e comparavel entre equipamentos de portes diferentes. As chaves sao os
    # nomes canonicos de 'maquinas', entao o conteudo e conferido em
    # _validar_valores, onde da para cruzar as duas secoes.
    ("metas_produtividade", (dict,)),

    ("painel", (dict,)),
    ("painel.titulo", (str,)),
    ("painel.arquivo", (str,)),
    ("painel.top_atrasos", (int,)),
    ("painel.cores", (dict,)),
    ("painel.cores.fundo", (str,)),
    ("painel.cores.cartao", (str,)),
    ("painel.cores.verde", (str,)),
    ("painel.cores.verde_medio", (str,)),
    ("painel.cores.vermelho", (str,)),
    ("painel.cores.texto", (str,)),
    ("painel.cores.texto_fraco", (str,)),
)


class ErroConfiguracao(Exception):
    """
    Configuracao ausente, malformada ou com valor invalido.

    Quem captura essa excecao no topo do programa termina com o codigo de
    saida CODIGO_SAIDA_ERRO_CONFIG.
    """


def _nomes_tipos(tipos: tuple[type, ...]) -> str:
    """Descreve os tipos aceitos em portugues, para a mensagem de erro."""
    traducao = {
        str: "texto",
        int: "numero inteiro",
        float: "numero decimal",
        list: "lista",
        dict: "mapa (chave: valor)",
    }
    return " ou ".join(traducao[t] for t in tipos)


def _buscar(dados: dict, caminho_chave: str) -> tuple[bool, Any]:
    """
    Busca um valor por caminho pontuado ("painel.cores.fundo").

    Devolve (True, valor) se achou. Devolve (False, None) se alguma parte do
    caminho nao existe ou se um no intermediario nao e um mapa - nos dois
    casos nao ha valor para entregar.
    """
    atual: Any = dados
    for parte in caminho_chave.split("."):
        if not isinstance(atual, dict) or parte not in atual:
            return False, None
        atual = atual[parte]
    return True, atual


def _tipo_aceito(valor: Any, tipos: tuple[type, ...]) -> bool:
    """
    Verifica o tipo do valor.

    True e False sao instancias de int em Python. Sem essa guarda,
    'taxa_sujeira: true' passaria despercebido e viraria 1.0 la na frente.
    """
    if isinstance(valor, bool):
        return bool in tipos
    return isinstance(valor, tipos)


def _validar_chaves(dados: dict) -> list[str]:
    """Confere presenca, tipo e nao-vazio de todas as chaves obrigatorias."""
    problemas: list[str] = []
    com_problema: set[str] = set()

    for caminho_chave, tipos in CHAVES_OBRIGATORIAS:
        pai = caminho_chave.rsplit(".", 1)[0] if "." in caminho_chave else ""
        if pai in com_problema:
            # O pai ja foi reportado. Repetir o erro em cada filho so polui.
            com_problema.add(caminho_chave)
            continue

        encontrado, valor = _buscar(dados, caminho_chave)
        if not encontrado:
            problemas.append(f"chave obrigatoria ausente: '{caminho_chave}'")
            com_problema.add(caminho_chave)
            continue

        if not _tipo_aceito(valor, tipos):
            problemas.append(
                f"chave '{caminho_chave}': esperado {_nomes_tipos(tipos)}, "
                f"veio {type(valor).__name__} com valor {valor!r}"
            )
            com_problema.add(caminho_chave)
            continue

        if isinstance(valor, (str, list, dict)) and len(valor) == 0:
            problemas.append(
                f"chave '{caminho_chave}': preenchida mas vazia "
                f"({type(valor).__name__} sem conteudo)"
            )
            com_problema.add(caminho_chave)

    return problemas


def _validar_cobertura(
    nome_secao: str,
    chaves_secao,
    nome_referencia: str,
    chaves_referencia,
) -> list[str]:
    """
    Confere que uma secao cobre exatamente as chaves de outra.

    Os dois sentidos importam. Item da referencia sem entrada aqui fica sem
    parametro; entrada com o nome digitado diferente nunca alcanca item nenhum.
    Nos dois casos o efeito e silencioso: o gerador ou o painel simplesmente
    ignoram aquele item, sem erro.
    """
    problemas = []
    faltando = set(chaves_referencia) - set(chaves_secao)
    sobrando = set(chaves_secao) - set(chaves_referencia)

    for chave in sorted(faltando, key=str):
        problemas.append(
            f"chave '{nome_secao}': {chave!r} existe em '{nome_referencia}' mas nao "
            f"tem entrada aqui"
        )
    for chave in sorted(sobrando, key=str):
        problemas.append(
            f"chave '{nome_secao}': {chave!r} nao existe em '{nome_referencia}'. "
            f"Validos: {', '.join(sorted((str(c) for c in chaves_referencia)))}"
        )
    return problemas


def _validar_faixas(
    nome_secao: str,
    faixas: dict,
    minimo_permitido: float,
    maximo_permitido: float,
) -> list[str]:
    """Confere pares [minimo, maximo] de uma secao inteira do config."""
    problemas = []
    for chave, faixa in faixas.items():
        rotulo = f"chave '{nome_secao}.{chave}'"

        if not isinstance(faixa, list) or len(faixa) != 2:
            problemas.append(
                f"{rotulo}: esperado uma lista [minimo, maximo], veio {faixa!r}"
            )
            continue

        inicio, fim = faixa
        if not (_tipo_aceito(inicio, (int, float)) and _tipo_aceito(fim, (int, float))):
            problemas.append(
                f"{rotulo}: os dois limites precisam ser numeros, veio {faixa!r}"
            )
            continue

        if inicio > fim:
            problemas.append(
                f"{rotulo}: minimo {inicio!r} e maior que o maximo {fim!r}"
            )
        if inicio < minimo_permitido or fim > maximo_permitido:
            problemas.append(
                f"{rotulo}: {faixa!r} fora dos limites aceitos "
                f"[{minimo_permitido}, {maximo_permitido}]"
            )
    return problemas


def _validar_cruzamentos(dados: dict) -> list[str]:
    """
    Confere que as secoes que se referenciam por nome estao em sincronia.

    Toda vez que uma chave do config e o nome de algo declarado em outra secao,
    um erro de digitacao produz silencio em vez de erro. Este e o unico lugar
    que pega isso.
    """
    problemas: list[str] = []
    gerador = dados["gerador"]

    # Coluna marcada como obrigatoria precisa ser coluna que a leitura conhece,
    # senao a regra de validacao nunca dispara. Aqui o cruzamento e de sentido
    # unico: coluna que nao e obrigatoria e o caso normal, nao um problema.
    for campo in sorted(set(dados["validacao"]["obrigatorios"]) - set(dados["colunas"]), key=str):
        problemas.append(
            f"chave 'validacao.obrigatorios': {campo!r} nao existe em 'colunas'. "
            f"Disponiveis: {', '.join(sorted(dados['colunas']))}"
        )

    # Marcador vazio ou so com espaco casaria com toda celula em branco e a
    # leitura descartaria a planilha inteira como rodape.
    for indice, marcador in enumerate(dados["marcadores_rodape"]):
        if not isinstance(marcador, str) or not marcador.strip():
            problemas.append(
                f"chave 'marcadores_rodape': item {indice} e {marcador!r}. "
                f"Cada marcador precisa ser um texto com conteudo"
            )

    # O mesmo apelido em duas colunas deixa a leitura sem criterio para decidir
    # de quem e a coluna 'TIRAGEM' da planilha. Escolher uma seria arbitrario e
    # silencioso, entao o carregamento recusa.
    dono_do_apelido: dict[str, str] = {}
    for campo, apelidos in dados["colunas"].items():
        for apelido in apelidos:
            chave = " ".join(str(apelido).upper().split())
            if chave in dono_do_apelido:
                problemas.append(
                    f"chave 'colunas': o apelido {apelido!r} esta em "
                    f"{dono_do_apelido[chave]!r} e tambem em {campo!r}. "
                    f"A leitura nao teria como decidir de quem e a coluna"
                )
            else:
                dono_do_apelido[chave] = campo

    problemas += _validar_cobertura(
        "metas_produtividade", dados["metas_produtividade"], "maquinas", dados["maquinas"]
    )
    problemas += _validar_cobertura(
        "gerador.setup_por_maquina", gerador["setup_por_maquina"], "maquinas", dados["maquinas"]
    )
    problemas += _validar_cobertura(
        "gerador.refugo_por_maquina", gerador["refugo_por_maquina"], "maquinas", dados["maquinas"]
    )
    problemas += _validar_cobertura(
        "gerador.tiragem_por_produto", gerador["tiragem_por_produto"],
        "gerador.produtos", gerador["produtos"],
    )

    for maquina in gerador["maquinas_digitais"]:
        if maquina not in dados["maquinas"]:
            problemas.append(
                f"chave 'gerador.maquinas_digitais': {maquina!r} nao e nome canonico "
                f"em 'maquinas'. Canonicos: {', '.join(sorted(dados['maquinas']))}"
            )
    if len(gerador["maquinas_digitais"]) >= len(dados["maquinas"]):
        problemas.append(
            f"chave 'gerador.maquinas_digitais': {len(gerador['maquinas_digitais'])} de "
            f"{len(dados['maquinas'])} maquinas sao digitais, entao nao sobra offset para "
            f"tiragem acima de 'tiragem_maxima_digital' e nenhuma ordem longa teria onde rodar"
        )

    # As tres fatias do fechamento saem de um sorteio so, entao as duas
    # declaradas nao podem passar de 100% - a terceira e o que sobra.
    fechamento = gerador["fechamento_tiragem"]
    soma_fatias = fechamento["prob_exato"] + fechamento["prob_abaixo_do_pedido"]
    if soma_fatias > 1:
        problemas.append(
            f"chave 'gerador.fechamento_tiragem': prob_exato "
            f"({fechamento['prob_exato']}) mais prob_abaixo_do_pedido "
            f"({fechamento['prob_abaixo_do_pedido']}) somam {soma_fatias}, acima de 1. "
            f"Nao sobraria fatia para a ordem que fecha acima do pedido"
        )

    concentracao = gerador["atrasos"]["concentracao"]
    if concentracao["maquina"] not in dados["maquinas"]:
        problemas.append(
            f"chave 'gerador.atrasos.concentracao.maquina': {concentracao['maquina']!r} "
            f"nao e nome canonico em 'maquinas'. "
            f"Canonicos: {', '.join(sorted(dados['maquinas']))}"
        )
    mes = concentracao["mes"]
    if len(mes) != 7 or mes[4] != "-" or not (mes[:4] + mes[5:]).isdigit():
        problemas.append(
            f"chave 'gerador.atrasos.concentracao.mes': {mes!r} nao esta no formato AAAA-MM"
        )

    return problemas


def _validar_escalares(dados: dict) -> list[str]:
    """
    Confere os valores soltos que tem faixa ou dominio conhecido.

    Presenca e tipo nao bastam: 'taxa_rejeicao_limite: 10' passa como numero
    e significa 1000% de rejeicao, o que desliga a trava da Decisao 2 sem que
    ninguem perceba.
    """
    problemas: list[str] = []
    gerador = dados["gerador"]

    nivel = dados["log"]["nivel"]
    if nivel.upper() not in NIVEIS_LOG_ACEITOS:
        problemas.append(
            f"chave 'log.nivel': valor {nivel!r} nao e um nivel valido. "
            f"Aceitos: {', '.join(NIVEIS_LOG_ACEITOS)}"
        )

    fracoes = (
        ("gerador.taxa_sujeira", gerador["taxa_sujeira"]),
        ("gerador.fechamento_tiragem.prob_abaixo_do_pedido",
         gerador["fechamento_tiragem"]["prob_abaixo_do_pedido"]),
        ("gerador.fechamento_tiragem.prob_exato",
         gerador["fechamento_tiragem"]["prob_exato"]),
        ("gerador.status.prob_em_producao", gerador["status"]["prob_em_producao"]),
        ("gerador.status.prob_cancelada", gerador["status"]["prob_cancelada"]),
        ("validacao.taxa_rejeicao_limite", dados["validacao"]["taxa_rejeicao_limite"]),
        ("validacao.refugo_maximo_sobre_tiragem",
         dados["validacao"]["refugo_maximo_sobre_tiragem"]),
    )
    for caminho_chave, valor in fracoes:
        if not 0 <= valor <= 1:
            problemas.append(
                f"chave '{caminho_chave}': valor {valor!r} fora da faixa. "
                f"E uma fracao, precisa estar entre 0 e 1"
            )

    # Meta de percentual e comparada contra indicador na escala 0-100. Um valor
    # fora dessa faixa pinta o painel inteiro de uma cor so, sem erro nenhum.
    # A faixa nao pega tudo: 'otd_minimo_pct: 0.95' escrito como fracao continua
    # sendo um numero valido entre 0 e 100, e nao ha como distinguir isso de uma
    # meta deliberadamente baixa.
    percentuais = (
        ("metas.refugo_maximo_pct", dados["metas"]["refugo_maximo_pct"]),
        ("metas.otd_minimo_pct", dados["metas"]["otd_minimo_pct"]),
        ("metas.aproveitamento_minimo_pct", dados["metas"]["aproveitamento_minimo_pct"]),
        ("metas.maquinas_na_meta_minimo_pct",
         dados["metas"]["maquinas_na_meta_minimo_pct"]),
        ("gerador.atrasos.otd_alvo_pct", gerador["atrasos"]["otd_alvo_pct"]),
        ("gerador.atrasos.concentracao.otd_pct",
         gerador["atrasos"]["concentracao"]["otd_pct"]),
        ("gerador.fechamento_tiragem.sobra_maxima_pct",
         gerador["fechamento_tiragem"]["sobra_maxima_pct"]),
        ("gerador.fechamento_tiragem.falta_maxima_pct",
         gerador["fechamento_tiragem"]["falta_maxima_pct"]),
    )
    for caminho_chave, valor in percentuais:
        if not 0 <= valor <= 100:
            problemas.append(
                f"chave '{caminho_chave}': valor {valor!r} fora da faixa. "
                f"E um percentual, precisa estar entre 0 e 100"
            )

    positivos = (
        ("gerador.arquivos", gerador["arquivos"]),
        ("metas.setup_maximo_min", dados["metas"]["setup_maximo_min"]),
        ("gerador.meses", gerador["meses"]),
        ("gerador.ordens_por_mes", gerador["ordens_por_mes"]),
        ("gerador.tiragem_maxima_digital", gerador["tiragem_maxima_digital"]),
        ("gerador.peso_digital_tiragem_curta", gerador["peso_digital_tiragem_curta"]),
        ("gerador.status.dias_janela_em_producao",
         gerador["status"]["dias_janela_em_producao"]),
        ("linhas_busca_cabecalho", dados["linhas_busca_cabecalho"]),
        ("validacao.tempo_maximo_minutos", dados["validacao"]["tempo_maximo_minutos"]),
        ("painel.top_atrasos", dados["painel"]["top_atrasos"]),
    )
    for caminho_chave, valor in positivos:
        if valor <= 0:
            problemas.append(
                f"chave '{caminho_chave}': valor {valor!r} precisa ser maior que zero"
            )

    for maquina, meta in dados["metas_produtividade"].items():
        if not _tipo_aceito(meta, (int, float)):
            problemas.append(
                f"chave 'metas_produtividade.{maquina}': esperado "
                f"{_nomes_tipos((int, float))}, veio {type(meta).__name__} "
                f"com valor {meta!r}"
            )
        elif meta <= 0:
            problemas.append(
                f"chave 'metas_produtividade.{maquina}': valor {meta!r} precisa ser "
                f"maior que zero (a meta e em folhas boas por hora)"
            )

    problemas += _validar_faixas(
        "gerador.tiragem_por_produto", gerador["tiragem_por_produto"], 1, 1_000_000
    )
    problemas += _validar_faixas(
        "gerador.setup_por_maquina", gerador["setup_por_maquina"],
        0, dados["validacao"]["tempo_maximo_minutos"],
    )
    problemas += _validar_faixas(
        "gerador.refugo_por_maquina", gerador["refugo_por_maquina"], 0, 100
    )
    problemas += _validar_faixas(
        "gerador.atrasos", {"dias_atraso": gerador["atrasos"]["dias_atraso"]}, 1, 3650
    )

    return problemas


def _validar_valores(dados: dict) -> list[str]:
    """Junta as conferencias de valor: escalares soltos, faixas e cruzamentos."""
    return _validar_escalares(dados) + _validar_cruzamentos(dados)


@dataclass(frozen=True)
class Configuracao:
    """
    Configuracao carregada e validada.

    As secoes ficam como dicionarios, no mesmo formato do YAML. E deliberado:
    o config.yaml e o contrato, e transformar cada secao numa classe propria
    criaria uma segunda estrutura para manter em sincronia com a primeira.
    """

    arquivo: Path
    raiz: Path
    caminhos: dict[str, str]
    log: dict[str, str]
    gerador: dict[str, Any]
    colunas: dict[str, list[str]]
    linhas_busca_cabecalho: int
    marcadores_rodape: list[str]
    maquinas: dict[str, list[str]]
    turnos: dict[str, list[str]]
    status: dict[str, list[str]]
    validacao: dict[str, Any]
    metas: dict[str, float]
    metas_produtividade: dict[str, float]
    painel: dict[str, Any]

    def caminho(self, chave: str) -> Path:
        """
        Resolve uma entrada da secao 'caminhos' em caminho absoluto.

        Caminho relativo no YAML e relativo a pasta do proprio config.yaml,
        nao ao diretorio de onde o comando foi disparado. Assim o programa
        aponta para os mesmos lugares independente de onde foi chamado.
        """
        if chave not in self.caminhos:
            raise ErroConfiguracao(
                f"caminho {chave!r} nao existe na secao 'caminhos' de {self.arquivo}. "
                f"Disponiveis: {', '.join(sorted(self.caminhos))}"
            )
        bruto = Path(self.caminhos[chave])
        return bruto if bruto.is_absolute() else (self.raiz / bruto).resolve()

    def arquivo_log(self) -> Path:
        """Caminho completo do arquivo de log."""
        return self.caminho("logs") / self.log["arquivo"]

    def arquivo_painel(self) -> Path:
        """Caminho completo do painel HTML."""
        return self.caminho("saida") / self.painel["arquivo"]

    def garantir_pastas(self) -> list[Path]:
        """
        Cria as pastas de trabalho que faltam e devolve as que foram criadas.

        Necessario porque 'saida/' e 'logs/' estao no .gitignore: quem clonar
        o repositorio nao recebe essas pastas.
        """
        alvos = [
            self.caminho("entrada"),
            self.caminho("saida"),
            self.caminho("logs"),
            self.caminho("banco").parent,
        ]
        criadas = []
        for pasta in alvos:
            if not pasta.exists():
                pasta.mkdir(parents=True)
                criadas.append(pasta)
        return criadas


def carregar(arquivo: Path | str = CONFIG_PADRAO) -> Configuracao:
    """
    Le o YAML, valida e devolve a configuracao.

    Levanta ErroConfiguracao com o caminho do arquivo e a lista completa de
    problemas encontrados - todos de uma vez, para nao obrigar a corrigir uma
    chave por execucao.
    """
    arquivo = Path(arquivo).resolve()

    if not arquivo.is_file():
        raise ErroConfiguracao(
            f"arquivo de configuracao nao encontrado: {arquivo}"
        )

    try:
        texto = arquivo.read_text(encoding="utf-8")
    except OSError as erro:
        raise ErroConfiguracao(
            f"nao foi possivel ler {arquivo}: {erro}"
        ) from erro

    try:
        dados = yaml.safe_load(texto)
    except yaml.YAMLError as erro:
        raise ErroConfiguracao(
            f"YAML invalido em {arquivo}: {erro}"
        ) from erro

    if not isinstance(dados, dict):
        raise ErroConfiguracao(
            f"{arquivo} deveria conter um mapa de chaves no topo, "
            f"mas contem {type(dados).__name__} com valor {dados!r}"
        )

    problemas = _validar_chaves(dados)
    if not problemas:
        # So vale conferir faixa de valor depois que presenca e tipo passaram.
        problemas = _validar_valores(dados)

    if problemas:
        lista = "\n".join(f"  - {p}" for p in problemas)
        raise ErroConfiguracao(
            f"configuracao invalida em {arquivo}:\n{lista}"
        )

    return Configuracao(
        arquivo=arquivo,
        raiz=arquivo.parent,
        caminhos=dados["caminhos"],
        log=dados["log"],
        gerador=dados["gerador"],
        colunas=dados["colunas"],
        linhas_busca_cabecalho=dados["linhas_busca_cabecalho"],
        marcadores_rodape=dados["marcadores_rodape"],
        maquinas=dados["maquinas"],
        turnos=dados["turnos"],
        status=dados["status"],
        validacao=dados["validacao"],
        metas=dados["metas"],
        metas_produtividade=dados["metas_produtividade"],
        painel=dados["painel"],
    )


def descrever(cfg: Configuracao) -> str:
    """Monta o resumo legivel da configuracao carregada."""
    linhas = [
        f"Arquivo:  {cfg.arquivo}",
        f"Raiz:     {cfg.raiz}",
        "",
        "Caminhos resolvidos",
        f"  entrada: {cfg.caminho('entrada')}",
        f"  banco:   {cfg.caminho('banco')}",
        f"  saida:   {cfg.caminho('saida')}",
        f"  logs:    {cfg.caminho('logs')}",
        f"  log:     {cfg.arquivo_log()}",
        f"  painel:  {cfg.arquivo_painel()}",
        "",
        "Log",
        f"  nivel: {cfg.log['nivel']}",
        "",
        "Gerador",
        f"  semente: {cfg.gerador['semente']}   arquivos: {cfg.gerador['arquivos']}"
        f"   meses: {cfg.gerador['meses']}",
        f"  ordens por mes: {cfg.gerador['ordens_por_mes']}"
        f"   taxa de sujeira: {cfg.gerador['taxa_sujeira']:.0%}",
        f"  clientes: {len(cfg.gerador['clientes'])}"
        f"   produtos: {len(cfg.gerador['produtos'])}"
        f"   operadores: {len(cfg.gerador['operadores'])}",
        "",
        f"Colunas mapeadas ({len(cfg.colunas)})",
    ]
    for campo, apelidos in cfg.colunas.items():
        linhas.append(f"  {campo:<20} <- {', '.join(apelidos)}")

    linhas.append("")
    linhas.append("Leitura")
    linhas.append(f"  linhas de busca do cabecalho: {cfg.linhas_busca_cabecalho}")
    linhas.append(f"  marcadores de rodape: {', '.join(cfg.marcadores_rodape)}")

    linhas.append("")
    linhas.append(f"Maquinas canonicas ({len(cfg.maquinas)})")
    linhas.append(
        f"  {'nome':<22} {'meta folhas/h':>13} {'setup min':>11} {'refugo %':>11}  tipo"
    )
    digitais = cfg.gerador["maquinas_digitais"]
    for nome, variantes in cfg.maquinas.items():
        # A validacao ja garantiu que toda maquina canonica tem meta e faixas.
        meta = f"{cfg.metas_produtividade[nome]:,.0f}".replace(",", ".")
        setup = cfg.gerador["setup_por_maquina"][nome]
        refugo = cfg.gerador["refugo_por_maquina"][nome]
        tipo = "digital" if nome in digitais else "offset"
        linhas.append(
            f"  {nome:<22} {meta:>13} {f'{setup[0]}-{setup[1]}':>11} "
            f"{f'{refugo[0]}-{refugo[1]}':>11}  {tipo} ({len(variantes)} variantes)"
        )

    linhas.append("")
    linhas.append(f"Tiragem por produto ({len(cfg.gerador['tiragem_por_produto'])}), em folhas")
    for produto, (minimo, maximo) in cfg.gerador["tiragem_por_produto"].items():
        linhas.append(f"  {produto:<40} {minimo:>7} a {maximo:>7}")

    linhas.append("")
    linhas.append(f"Turnos canonicos ({len(cfg.turnos)})")
    for nome, variantes in cfg.turnos.items():
        linhas.append(f"  {nome:<26} <- {', '.join(variantes)}")

    linhas.append("")
    linhas.append(f"Status canonicos ({len(cfg.status)})")
    for nome, variantes in cfg.status.items():
        linhas.append(f"  {nome:<26} <- {', '.join(variantes)}")

    atrasos = cfg.gerador["atrasos"]
    concentracao = atrasos["concentracao"]
    linhas.append("")
    linhas.append("Gerador: atrasos e fechamento")
    linhas.append(f"  OTD alvo do historico: {atrasos['otd_alvo_pct']}%")
    linhas.append(
        f"  concentracao: {concentracao['maquina']} em {concentracao['mes']} "
        f"com OTD {concentracao['otd_pct']}%"
    )
    linhas.append(
        f"  atraso de {atrasos['dias_atraso'][0]} a {atrasos['dias_atraso'][1]} dias "
        f"alem do prazo"
    )
    fechamento = cfg.gerador["fechamento_tiragem"]
    linhas.append(
        f"  sobra de ate {fechamento['sobra_maxima_pct']}%, "
        f"{fechamento['prob_abaixo_do_pedido']:.0%} das ordens fecham abaixo "
        f"(falta de ate {fechamento['falta_maxima_pct']}%)"
    )
    linhas.append(
        f"  digital ate {cfg.gerador['tiragem_maxima_digital']} folhas, "
        f"peso {cfg.gerador['peso_digital_tiragem_curta']}x na tiragem curta"
    )

    linhas.append("")
    linhas.append("Validacao")
    linhas.append(f"  obrigatorios: {', '.join(cfg.validacao['obrigatorios'])}")
    linhas.append(
        f"  refugo maximo sobre tiragem: "
        f"{cfg.validacao['refugo_maximo_sobre_tiragem']:.0%}"
    )
    linhas.append(f"  tempo maximo por ordem: {cfg.validacao['tempo_maximo_minutos']} min")
    linhas.append(
        f"  limite de rejeicao: {cfg.validacao['taxa_rejeicao_limite']:.0%} "
        f"(acima disso o programa sai com codigo 1)"
    )

    linhas.append("")
    linhas.append("Metas")
    for nome, valor in cfg.metas.items():
        linhas.append(f"  {nome:<28} {valor}")

    linhas.append("")
    linhas.append("Painel")
    linhas.append(f"  titulo: {cfg.painel['titulo']}")
    linhas.append(f"  top atrasos: {cfg.painel['top_atrasos']}")
    linhas.append(f"  cores: {', '.join(f'{k}={v}' for k, v in cfg.painel['cores'].items())}")

    return "\n".join(linhas)


def main(argumentos: list[str]) -> int:
    """
    Entrada de 'python -m src.config [caminho.yaml]'.

    Carrega, cria as pastas de trabalho e imprime a configuracao.
    """
    arquivo = Path(argumentos[0]) if argumentos else CONFIG_PADRAO

    try:
        cfg = carregar(arquivo)
    except ErroConfiguracao as erro:
        print(f"ERRO DE CONFIGURACAO: {erro}", file=sys.stderr)
        return CODIGO_SAIDA_ERRO_CONFIG

    print(descrever(cfg))

    criadas = cfg.garantir_pastas()
    print("")
    if criadas:
        print("Pastas de trabalho criadas agora:")
        for pasta in criadas:
            print(f"  {pasta}")
    else:
        print("Pastas de trabalho: todas ja existiam.")

    print("")
    print("Configuracao carregada e validada com sucesso.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
