"""
Limpeza e normalizacao dos valores crus que vem da leitura.

Funcoes pequenas e independentes: cada uma recebe um valor, devolve o valor
limpo E a descricao do que foi feito. Nenhuma delas conhece pandas, planilha ou
banco - da para testar todas sem ter arquivo nenhum.

Tres estados possiveis no retorno, e so tres:

- nada a corrigir      -> valor preenchido, correcao None, problema None
- corrigido            -> valor preenchido, correcao descrevendo o que era
- nao deu para limpar  -> valor None, problema dizendo por que, com o valor cru

Celula vazia nao e nenhum dos tres: devolve None limpo, sem correcao e sem
problema. Vazio nao e erro de formato - se aquele campo era obrigatorio, quem
decide e a validacao, no Bloco 5.
"""

import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

# Origem do sistema de datas do Excel de 1900, padrao no Windows. A base e
# 30/12/1899 e nao 31/12/1899 porque o Excel conta 1900 como bissexto - um bug
# de compatibilidade que nunca foi corrigido. Confere com as ancoras conhecidas
# (2000-01-01 = 36526, 2024-01-01 = 45292). Planilha salva no sistema de datas
# de 1904 (Mac antigo) sairia com quatro anos de erro; a secao 11 assume 1900.
BASE_SERIAL_EXCEL = date(1899, 12, 30)

MESES_ABREV = (
    "jan", "fev", "mar", "abr", "mai", "jun",
    "jul", "ago", "set", "out", "nov", "dez",
)

# Cerca de sanidade para o serial do Excel. Sem ela, um 5000 digitado por engano
# numa coluna de data viraria 03/09/1913 sem uma palavra de aviso. Nao virou
# config por ser limite de plausibilidade do formato, nao parametro da fabrica -
# se um dia precisar importar historico antigo, e aqui que se mexe.
FAIXA_ANOS_SERIAL = (1990, 2100)

# Ano de dois digitos: 26 e 2026, 98 e 1998. A virada em 70 e a convencao usada
# pelo proprio Excel.
CORTE_SECULO = 70

# Formatos de data em texto que a fabrica realmente produz, na ordem de
# tentativa. DD/MM vem antes de qualquer leitura americana: e o formato do
# teclado brasileiro e o padrao da planilha em pt-BR.
FORMATOS_DATA = (
    ("%Y-%m-%d", "AAAA-MM-DD"),
    ("%d/%m/%Y", "DD/MM/AAAA"),
    ("%d/%m/%y", "DD/MM/AA"),
    ("%d-%m-%Y", "DD-MM-AAAA"),
    ("%Y/%m/%d", "AAAA/MM/DD"),
)

# '5-mar-26', '5-marco-2026', '15-DEZ-25'.
PADRAO_DATA_ABREVIADA = re.compile(r"^(\d{1,2})-([a-z]+)-(\d{2,4})$")

# Tipo numerico que cada coluna espera, espelhando o esquema do banco da secao
# 4.3: tiragem e refugo sao INTEGER, tempo e REAL.
#
# Nao esta no config.yaml de proposito. O que a fabrica renomeia e o nome da
# coluna NA PLANILHA, e isso ja e configuravel pelos apelidos em 'colunas'. A
# chave aqui e o nome canonico interno, que e contrato entre os modulos e muda
# junto com o esquema do banco - se um dia mudar, o teste de cobertura no
# tests/test_limpeza.py acusa.
TIPO_NUMERICO_POR_COLUNA: dict[str, type] = {
    "tiragem_prevista": int,
    "tiragem_boa": int,
    "refugo": int,
    "tempo_setup_min": float,
    "tempo_producao_min": float,
}

# Status que o pipeline conhece pelo SIGNIFICADO, nao so pelo nome: o gerador
# precisa saber qual deles nao produz folha, e a validacao precisa saber qual
# deles exige data de entrega. Ficam aqui pelo mesmo motivo de
# TIPO_NUMERICO_POR_COLUNA - sao contrato entre os modulos, e duas copias sairiam
# de sincronia na primeira renomeacao.
#
# O nome esta cravado, o vocabulario nao: quem digita 'Ok' ou 'em produção' e
# reconhecido pelas variantes da secao 'status' do config.yaml. Cada modulo que
# usa estas constantes confere antes de rodar que elas existem la.
STATUS_EM_PRODUCAO = "EM PRODUCAO"
STATUS_ENTREGUE = "ENTREGUE"
STATUS_CANCELADA = "CANCELADA"


@dataclass(frozen=True)
class ValorLimpo:
    """O valor depois da limpeza, com o rastro do que aconteceu com ele."""

    valor: Any
    correcao: str | None = None
    problema: str | None = None


def sem_acento(texto: str) -> str:
    """
    Remove acento, preservando a letra.

    Necessario porque o operador digita 'em produção' e o config declara
    'em producao'. Usado so para comparar, nunca para gravar: o texto que vai
    para o banco mantem o acento que tinha.
    """
    decomposto = unicodedata.normalize("NFKD", texto)
    return "".join(c for c in decomposto if not unicodedata.combining(c))


def chave_comparacao(valor: Any) -> str:
    """
    Poe um valor na forma usada para casar com sinonimo do config.

    Minuscula, sem acento, sem espaco sobrando e sem espaco duplicado no meio.
    'HEIDELBERG  SM 52 ' e 'heidelberg sm 52' viram a mesma chave.
    """
    return " ".join(sem_acento(str(valor)).casefold().split())


def normalizar_texto(valor: Any) -> ValorLimpo:
    """
    Tira espaco da ponta e colapsa espaco duplicado no meio.

    Nao mexe em caixa nem em acento: 'Folder A4 4x4 couche 150g' precisa chegar
    ao painel como foi escrito. Quem precisa de forma comparavel usa
    chave_comparacao.
    """
    if valor is None:
        return ValorLimpo(None)

    if not isinstance(valor, str):
        # Numero ou data numa coluna de texto. Converter e melhor que recusar,
        # mas fica registrado: pode ser sintoma de coluna trocada na planilha.
        texto = " ".join(str(valor).split())
        return ValorLimpo(
            texto or None,
            correcao=f"valor nao textual ({type(valor).__name__}) convertido para texto",
        )

    texto = " ".join(valor.split())
    if not texto:
        return ValorLimpo(None)
    if texto != valor:
        return ValorLimpo(texto, correcao="espaco sobrando removido")
    return ValorLimpo(texto)


def _compactar(chave: str) -> str:
    """
    Tira todo separador da chave: 'heidelberg sm-52' e 'sm 52' viram 'sm52'.

    E o segundo passo do casamento. O operador poe espaco e hifen onde quer, e
    seria interminavel declarar no config toda combinacao de 'SM52', 'SM 52' e
    'SM-52'.
    """
    return re.sub(r"[\s\-_./]", "", chave)


def _chaves_aceitas(canonico: str, variantes: list[str]) -> set[str]:
    """
    Todas as formas comparaveis que representam um nome canonico.

    O proprio canonico entra como variante de si mesmo. Sem isso,
    'Polar 115 (Guilhotina)' nao casaria com a lista ['polar 115', 'polar'] e a
    maioria das linhas - que vem escrita certa - seria rejeitada.
    """
    chaves = {chave_comparacao(canonico)}
    chaves.update(chave_comparacao(v) for v in variantes)
    return chaves


def _casar_sinonimo(valor: Any, sinonimos: dict[str, list[str]], campo: str) -> ValorLimpo:
    """
    Casa um valor com o nome canonico, pelas variantes declaradas no config.

    Dois passos. Primeiro pela chave normalizada, que ja resolve caixa, acento e
    espaco sobrando. Se nao casar, pela chave compactada, que ignora tambem
    espaco e hifen internos - e o que faz 'HEIDELBERG  SM 52 ' encontrar
    'heidelberg sm52'.

    O passo compactado pode empatar: se duas maquinas diferentes compactarem
    para a mesma coisa, o resultado seria arbitrario. Nesse caso a funcao recusa
    e nomeia as candidatas, em vez de escolher uma.
    """
    if valor is None:
        return ValorLimpo(None)

    chave = chave_comparacao(valor)
    if not chave:
        return ValorLimpo(None)

    def resultado(canonico: str) -> ValorLimpo:
        if str(valor) == canonico:
            return ValorLimpo(canonico)
        return ValorLimpo(
            canonico, correcao=f"{campo} {valor!r} reconhecido como {canonico!r}"
        )

    for canonico, variantes in sinonimos.items():
        if chave in _chaves_aceitas(canonico, variantes):
            return resultado(canonico)

    compacta = _compactar(chave)
    candidatos = [
        canonico
        for canonico, variantes in sinonimos.items()
        if compacta in {_compactar(c) for c in _chaves_aceitas(canonico, variantes)}
    ]

    if len(candidatos) == 1:
        return resultado(candidatos[0])

    if len(candidatos) > 1:
        return ValorLimpo(
            None,
            problema=(
                f"{campo} {valor!r} e ambiguo: sem espaco e sem hifen ele casa com "
                f"{', '.join(repr(c) for c in sorted(candidatos))}. "
                f"Ajuste as variantes no config.yaml"
            ),
        )

    return ValorLimpo(
        None,
        problema=(
            f"{campo} {valor!r} nao casa com nenhum nome conhecido. "
            f"Canonicos: {', '.join(sorted(sinonimos))}"
        ),
    )


def normalizar_maquina(valor: Any, sinonimos: dict[str, list[str]]) -> ValorLimpo:
    """Reduz a variante digitada no chao de fabrica ao nome canonico da maquina."""
    return _casar_sinonimo(valor, sinonimos, "maquina")


def normalizar_turno(valor: Any, sinonimos: dict[str, list[str]]) -> ValorLimpo:
    """Reduz '1o', 'manha', 'A' ao turno canonico."""
    return _casar_sinonimo(valor, sinonimos, "turno")


def normalizar_status(valor: Any, sinonimos: dict[str, list[str]]) -> ValorLimpo:
    """Reduz 'Ok', 'concluida', 'em produção' ao status canonico."""
    return _casar_sinonimo(valor, sinonimos, "status")


def _data_de_serial(serial: float, original: Any) -> ValorLimpo:
    """Converte serial do Excel em data, recusando o que nao e data plausivel."""
    if float(serial) != int(serial):
        return ValorLimpo(
            None,
            problema=(
                f"data {original!r}: serial do Excel com casa decimal. "
                f"O inteiro e o dia, a fracao seria a hora, e este pipeline "
                f"trabalha so com data"
            ),
        )

    convertida = BASE_SERIAL_EXCEL + timedelta(days=int(serial))
    minimo, maximo = FAIXA_ANOS_SERIAL
    if not minimo <= convertida.year <= maximo:
        return ValorLimpo(
            None,
            problema=(
                f"data {original!r}: como serial do Excel daria {convertida.isoformat()}, "
                f"fora da faixa aceita de {minimo} a {maximo}. "
                f"Provavelmente nao e uma data"
            ),
        )

    return ValorLimpo(
        convertida, correcao=f"serial do Excel {int(serial)} convertido em data"
    )


def _data_abreviada(texto: str, original: Any) -> ValorLimpo | None:
    """
    Le '5-mar-26'. Devolve None quando o texto nao tem essa forma.

    O mes casa pelas tres primeiras letras, entao 'mar', 'marc' e 'marco'
    funcionam igual.
    """
    casamento = PADRAO_DATA_ABREVIADA.match(chave_comparacao(texto))
    if casamento is None:
        return None

    dia_txt, mes_txt, ano_txt = casamento.groups()
    prefixo = mes_txt[:3]
    if prefixo not in MESES_ABREV:
        return ValorLimpo(
            None,
            problema=(
                f"data {original!r}: {mes_txt!r} nao e um mes. "
                f"Aceitos: {', '.join(MESES_ABREV)}"
            ),
        )

    ano = int(ano_txt)
    if len(ano_txt) <= 2:
        ano += 2000 if ano < CORTE_SECULO else 1900

    try:
        convertida = date(ano, MESES_ABREV.index(prefixo) + 1, int(dia_txt))
    except ValueError as erro:
        return ValorLimpo(None, problema=f"data {original!r}: {erro}")

    return ValorLimpo(convertida, correcao="data no formato D-MMM-AA convertida")


def converter_data(valor: Any) -> ValorLimpo:
    """
    Converte para date o que a planilha entrega como data.

    Aceita date e datetime do proprio Excel, serial numerico, e texto nos
    formatos AAAA-MM-DD, DD/MM/AAAA, DD/MM/AA, DD-MM-AAAA, AAAA/MM/DD e
    D-MMM-AA.

    Ambiguidade assumida: '05/03/2026' e lido como 5 de marco, nunca 3 de maio.
    Nao ha como distinguir os dois numa celula isolada, e dd/mm e o formato do
    teclado brasileiro. Uma planilha exportada em locale americano seria lida
    errada sem erro nenhum - e a limitacao mais perigosa deste modulo.
    """
    if valor is None:
        return ValorLimpo(None)

    if isinstance(valor, bool):
        return ValorLimpo(None, problema=f"data {valor!r}: booleano nao e data")

    if isinstance(valor, datetime):
        if valor.time() != datetime.min.time():
            return ValorLimpo(
                valor.date(),
                correcao=f"hora {valor.time().isoformat()} descartada da data",
            )
        return ValorLimpo(valor.date())

    if isinstance(valor, date):
        return ValorLimpo(valor)

    if isinstance(valor, (int, float)):
        return _data_de_serial(valor, valor)

    if not isinstance(valor, str):
        return ValorLimpo(
            None,
            problema=f"data {valor!r}: tipo {type(valor).__name__} nao vira data",
        )

    texto = valor.strip()
    if not texto:
        return ValorLimpo(None)

    # Serial que chegou como texto, porque a celula estava formatada como texto.
    if re.fullmatch(r"-?\d+", texto):
        resultado = _data_de_serial(int(texto), valor)
        if resultado.problema is None:
            return ValorLimpo(
                resultado.valor,
                correcao=f"serial do Excel em texto {texto!r} convertido em data",
            )
        return resultado

    abreviada = _data_abreviada(texto, valor)
    if abreviada is not None:
        return abreviada

    for formato, rotulo in FORMATOS_DATA:
        try:
            convertida = datetime.strptime(texto, formato).date()
        except ValueError:
            continue
        return ValorLimpo(convertida, correcao=f"data no formato {rotulo} convertida")

    return ValorLimpo(
        None,
        problema=(
            f"data {valor!r} nao esta em nenhum formato conhecido. "
            f"Aceitos: {', '.join(r for _, r in FORMATOS_DATA)}, D-MMM-AA "
            f"e serial do Excel"
        ),
    )


def _no_tipo_pedido(
    numero: float, original: Any, tipo: type, correcao: str | None
) -> ValorLimpo:
    """
    Entrega o numero no tipo que a coluna declarou.

    Casa decimal numa coluna de contagem e recusada, nao arredondada: 5000,5
    folhas nao existe, e arredondar em silencio trocaria o dado errado por um
    dado plausivel que ninguem mais conseguiria auditar.
    """
    if tipo is int:
        if not float(numero).is_integer():
            return ValorLimpo(
                None,
                problema=(
                    f"numero {original!r}: a coluna e de contagem inteira e o valor "
                    f"tem casa decimal ({numero})"
                ),
            )
        return ValorLimpo(int(numero), correcao=correcao)
    return ValorLimpo(float(numero), correcao=correcao)


def _desfazer_separadores(texto: str, tipo: type) -> str:
    """
    Reduz o numero digitado a uma forma que float() entende.

    O espaco sai sempre - so serve como separador de milhar. Depois, cada
    separador restante e classificado como milhar ou decimal, e e aqui que o
    tipo esperado da coluna faz diferenca: numa contagem inteira nao ha
    ambiguidade a resolver, porque contagem nao tem casa decimal.
    """
    limpo = re.sub(r"\s+", "", texto.replace("\xa0", " "))

    # Separador repetido so pode ser milhar: '1.234.567', '1,234,567'.
    if limpo.count(",") > 1:
        limpo = limpo.replace(",", "")
    if limpo.count(".") > 1:
        limpo = limpo.replace(".", "")

    if "," in limpo and "." in limpo:
        # Os dois presentes: o ultimo a aparecer e o decimal.
        if limpo.rfind(",") > limpo.rfind("."):
            return limpo.replace(".", "").replace(",", ".")
        return limpo.replace(",", "")

    separador = "," if "," in limpo else ("." if "." in limpo else "")
    if not separador:
        return limpo

    depois = limpo.partition(separador)[2]

    if tipo is int:
        # Coluna de contagem: ponto e virgula sao sempre milhar, sem chute sobre
        # quantos digitos vem antes - '1234.567' e um milhao e duzentos mil, nao
        # mil duzentos e trinta e quatro virgula. O unico requisito e o grupo de
        # tres digitos, que e o que define um separador de milhar.
        if len(depois) == 3:
            return limpo.replace(separador, "")
        # Separador que nao forma grupo de tres nao e milhar. Em vez de apagar e
        # inventar outro numero ('41.6' viraria 416), deixa como decimal: a
        # conversao recusa por nao ser inteiro e o valor vai para o relatorio.
        return limpo.replace(",", ".")

    if separador == ",":
        # Virgula sozinha e decimal em pt-BR: '5000,0' e cinco mil.
        return limpo.replace(",", ".")

    # Coluna decimal com ponto sozinho: aqui a ambiguidade e real, porque o
    # valor pode legitimamente ter casa decimal. Ate tres digitos antes e
    # exatamente tres depois e milhar.
    antes = limpo.partition(".")[0].lstrip("+-")
    if len(depois) == 3 and 1 <= len(antes) <= 3 and antes.isdigit():
        return limpo.replace(".", "")
    return limpo


def converter_numero(valor: Any, tipo: type) -> ValorLimpo:
    """
    Converte para numero o que a planilha entrega como quantidade ou tempo.

    Aceita int e float direto, e texto com separador de milhar por ponto
    ('5.000'), por espaco ('5 000') e com virgula decimal ('5000,0').

    `tipo` e o que a coluna espera, int ou float, e vem de
    TIPO_NUMERICO_POR_COLUNA. Nao tem valor padrao de proposito: e justamente
    esse contexto que elimina a adivinhacao sobre o separador. Numa coluna de
    contagem, ponto e sempre milhar; a ambiguidade so sobra onde a coluna
    legitimamente aceita decimal.

    O retorno respeita o tipo pedido: int para contagem, float para tempo. Valor
    com casa decimal numa coluna de contagem vira problema, nao arredondamento -
    arredondar em silencio esconderia o dado errado.
    """
    if tipo not in (int, float):
        raise ValueError(
            f"converter_numero: 'tipo' precisa ser int ou float, recebido {tipo!r}. "
            f"Use TIPO_NUMERICO_POR_COLUNA para descobrir o da coluna"
        )

    if valor is None:
        return ValorLimpo(None)

    if isinstance(valor, bool):
        return ValorLimpo(
            None, problema=f"numero {valor!r}: booleano nao e quantidade"
        )

    if isinstance(valor, (int, float)):
        if valor != valor or valor in (float("inf"), float("-inf")):
            return ValorLimpo(None, problema=f"numero {valor!r} nao e finito")
        return _no_tipo_pedido(valor, valor, tipo, correcao=None)

    # datetime e subclasse de date, entao esta checagem cobre os dois.
    if isinstance(valor, date):
        return ValorLimpo(
            None,
            problema=f"numero {valor!r}: data numa coluna numerica",
        )

    if not isinstance(valor, str):
        return ValorLimpo(
            None,
            problema=f"numero {valor!r}: tipo {type(valor).__name__} nao vira numero",
        )

    texto = valor.strip()
    if not texto:
        return ValorLimpo(None)

    candidato = _desfazer_separadores(texto, tipo)
    try:
        numero = float(candidato)
    except ValueError:
        return ValorLimpo(
            None,
            problema=(
                f"numero {valor!r} nao e numerico. "
                f"Depois de tratar os separadores sobrou {candidato!r}"
            ),
        )

    if numero != numero or numero in (float("inf"), float("-inf")):
        return ValorLimpo(None, problema=f"numero {valor!r} nao e finito")

    return _no_tipo_pedido(
        numero, valor, tipo, correcao=f"numero em texto {valor!r} convertido"
    )
