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
    ("colunas.tiragem_prevista", (list,)),
    ("colunas.tiragem_boa", (list,)),
    ("colunas.refugo", (list,)),
    ("colunas.tempo_setup_min", (list,)),
    ("colunas.tempo_producao_min", (list,)),

    ("linhas_busca_cabecalho", (int,)),

    ("maquinas", (dict,)),
    ("turnos", (dict,)),

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


def _validar_valores(dados: dict) -> list[str]:
    """
    Confere os valores que tem faixa ou dominio conhecido.

    Presenca e tipo nao bastam: 'taxa_rejeicao_limite: 10' passa como numero
    e significa 1000% de rejeicao, o que desliga a trava da Decisao 2 sem que
    ninguem perceba.
    """
    problemas: list[str] = []

    nivel = dados["log"]["nivel"]
    if nivel.upper() not in NIVEIS_LOG_ACEITOS:
        problemas.append(
            f"chave 'log.nivel': valor {nivel!r} nao e um nivel valido. "
            f"Aceitos: {', '.join(NIVEIS_LOG_ACEITOS)}"
        )

    fracoes = (
        ("gerador.taxa_sujeira", dados["gerador"]["taxa_sujeira"]),
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

    positivos = (
        ("gerador.arquivos", dados["gerador"]["arquivos"]),
        ("gerador.meses", dados["gerador"]["meses"]),
        ("gerador.ordens_por_mes", dados["gerador"]["ordens_por_mes"]),
        ("linhas_busca_cabecalho", dados["linhas_busca_cabecalho"]),
        ("validacao.tempo_maximo_minutos", dados["validacao"]["tempo_maximo_minutos"]),
        ("painel.top_atrasos", dados["painel"]["top_atrasos"]),
    )
    for caminho_chave, valor in positivos:
        if valor <= 0:
            problemas.append(
                f"chave '{caminho_chave}': valor {valor!r} precisa ser maior que zero"
            )

    # As colunas marcadas como obrigatorias na validacao precisam ser colunas
    # que a leitura conhece, senao a regra nunca dispara e ninguem descobre.
    colunas_conhecidas = set(dados["colunas"].keys())
    for campo in dados["validacao"]["obrigatorios"]:
        if campo not in colunas_conhecidas:
            problemas.append(
                f"chave 'validacao.obrigatorios': {campo!r} nao existe em 'colunas'. "
                f"Disponiveis: {', '.join(sorted(colunas_conhecidas))}"
            )

    return problemas


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
    maquinas: dict[str, list[str]]
    turnos: dict[str, list[str]]
    validacao: dict[str, Any]
    metas: dict[str, float]
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
        maquinas=dados["maquinas"],
        turnos=dados["turnos"],
        validacao=dados["validacao"],
        metas=dados["metas"],
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
    linhas.append(f"Maquinas canonicas ({len(cfg.maquinas)})")
    for nome, variantes in cfg.maquinas.items():
        linhas.append(f"  {nome:<26} <- {len(variantes)} variantes")

    linhas.append("")
    linhas.append(f"Turnos canonicos ({len(cfg.turnos)})")
    for nome, variantes in cfg.turnos.items():
        linhas.append(f"  {nome:<26} <- {', '.join(variantes)}")

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
        linhas.append(f"  {nome:<26} {valor}")

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
