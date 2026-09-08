"""
Apoio compartilhado pelos testes de validacao e de banco.

A configuracao usada nos testes e montada aqui, pequena e completa, para o teste
falhar por causa do codigo e nao por causa de uma meta trocada no config.yaml.
Os testes que precisam do config de verdade o carregam explicitamente.
"""

import sys
from datetime import date
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import CONFIG_PADRAO, Configuracao
from src.leitura import COLUNA_ARQUIVO, COLUNA_LINHA
from src.limpeza import STATUS_CANCELADA, STATUS_EM_PRODUCAO, STATUS_ENTREGUE

COLUNAS = (
    "numero_os", "cliente", "produto", "maquina", "operador", "turno",
    "data_entrada", "data_prazo", "data_entrega", "status",
    "tiragem_prevista", "tiragem_boa", "refugo",
    "tempo_setup_min", "tempo_producao_min",
)

# Uma linha limpa e coerente, no formato que a leitura entrega e que a validacao
# aprova sem tocar. Cada teste parte dela e estraga uma celula so, para o que
# aparecer no resultado ser inequivocamente daquela celula.
LINHA_BOA = {
    "numero_os": "OS-2026-0001",
    "cliente": "CLI-001",
    "produto": "Folder A4 4x4 couche 150g",
    "maquina": "Heidelberg SM52",
    "operador": "OP-03",
    "turno": "1",
    "data_entrada": date(2026, 3, 2),
    "data_prazo": date(2026, 3, 12),
    "data_entrega": date(2026, 3, 10),
    "status": STATUS_ENTREGUE,
    "tiragem_prevista": 5000,
    "tiragem_boa": 5100,
    "refugo": 120,
    "tempo_setup_min": 30.0,
    "tempo_producao_min": 34.0,
}


def cfg_teste(**sobrepoe) -> Configuracao:
    """Monta uma Configuracao completa e minima, sem tocar no disco."""
    secoes = {
        "arquivo": CONFIG_PADRAO,
        "raiz": CONFIG_PADRAO.parent,
        "caminhos": {"entrada": "dados/entrada", "banco": "dados/producao.db",
                     "saida": "saida", "logs": "logs"},
        "log": {"nivel": "INFO", "arquivo": "execucao.log"},
        "gerador": {},
        "colunas": {coluna: [coluna.upper()] for coluna in COLUNAS},
        "linhas_busca_cabecalho": 12,
        "marcadores_rodape": ["TOTAL"],
        "maquinas": {"Heidelberg SM52": ["heidelberg sm52", "sm 52"]},
        "turnos": {"1": ["1", "manha"], "2": ["2", "tarde"]},
        "status": {
            STATUS_EM_PRODUCAO: ["em producao", "aberta"],
            STATUS_ENTREGUE: ["entregue", "ok"],
            STATUS_CANCELADA: ["cancelada", "canc"],
        },
        "validacao": {
            "obrigatorios": ["numero_os", "maquina", "data_entrada"],
            "refugo_maximo_sobre_tiragem": 0.5,
            "tempo_maximo_minutos": 2880,
            "taxa_rejeicao_limite": 0.10,
        },
        # As cinco metas que config.py exige. Os valores sao os mesmos do
        # config.yaml: o teste compara contra numero conhecido, e mudar a meta
        # real nao pode fazer teste de indicador falhar.
        "metas": {
            "refugo_maximo_pct": 3.0,
            "otd_minimo_pct": 95.0,
            "setup_maximo_min": 40.0,
            "aproveitamento_minimo_pct": 65.0,
            "maquinas_na_meta_minimo_pct": 75.0,
        },
        "metas_produtividade": {"Heidelberg SM52": 9000},
        "painel": {
            "titulo": "Painel de Producao Industrial",
            "arquivo": "painel.html",
            "top_atrasos": 10,
            "cores": {
                "fundo": "#060a0c",
                "cartao": "#0c1519",
                "verde": "#29e07f",
                "verde_medio": "#1f8f4e",
                "vermelho": "#e05a5a",
                "texto": "#e7efea",
                "texto_fraco": "#8fa79c",
            },
        },
    }
    secoes.update(sobrepoe)
    return Configuracao(**secoes)


def linhas(*alteracoes: dict) -> pd.DataFrame:
    """
    Monta o DataFrame no formato que a leitura entrega, uma linha por alteracao.

    Os valores ja saem limpos, entao o mesmo DataFrame serve como entrada da
    validacao e como lote aprovado para a gravacao - as duas camadas usam o
    mesmo conjunto de colunas.
    """
    registros = []
    for numero, alteracao in enumerate(alteracoes, start=2):
        registro = dict(LINHA_BOA)
        registro.update(alteracao)
        registro[COLUNA_ARQUIVO] = "planilha.xlsx"
        registro[COLUNA_LINHA] = numero
        registros.append(registro)
    return pd.DataFrame(
        registros, columns=list(COLUNAS) + [COLUNA_ARQUIVO, COLUNA_LINHA], dtype=object
    )
