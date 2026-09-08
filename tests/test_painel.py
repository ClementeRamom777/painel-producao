"""
Testes do painel HTML.

Tres coisas precisam ser verdade em toda geracao, e sao dificeis de ver a olho
num arquivo de 170 KB de base64:

1. O arquivo e autocontido. Nenhum src ou href que nao seja data:, nenhum
   script, nenhum @import. O criterio de aceite do bloco e abrir com a internet
   desligada, e um <img src="http://..."> passaria despercebido numa maquina
   conectada.

2. Todo texto vindo da planilha e escapado. Nome de produto e campo livre
   digitado no chao de fabrica.

3. O periodo incompleto e marcado. Sem marca, o painel afirma um OTD otimista
   como se fosse o numero final.
"""

import re
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from apoio import cfg_teste, linhas
from src.banco import conectar, criar_esquema, gravar_ordens, registrar_execucao
from src.indicadores import calcular
from src.limpeza import STATUS_CANCELADA, STATUS_EM_PRODUCAO
from src.painel import (
    ErroPainel,
    _numero,
    gerar,
    grafico_otd_mensal,
    grafico_produtividade,
    grafico_refugo,
    grafico_setup,
    montar_cartoes,
    montar_html,
    ordens_atrasadas,
    rotulo_acima_colide,
    ultima_importacao,
)

MOMENTO = "2026-09-08T10:00:00"


@pytest.fixture
def banco(tmp_path):
    con = conectar(tmp_path / "producao.db")
    criar_esquema(con)
    yield con
    con.close()


def povoar(banco, *alteracoes, cfg=None):
    """
    Grava as linhas e devolve os indicadores calculados sobre elas.

    O commit e necessario porque gerar() abre a propria conexao com o arquivo:
    sem ele, a transacao ficaria aberta nesta conexao e a outra veria um banco
    vazio. Em uso real quem faz o commit e a transacao unica de importar().
    """
    cfg = cfg_teste() if cfg is None else cfg
    gravar_ordens(banco, linhas(*alteracoes), cfg, MOMENTO)
    banco.commit()
    return calcular(banco)


def pagina(banco, *alteracoes, cfg=None):
    """Monta o HTML completo sobre as linhas dadas."""
    cfg = cfg_teste() if cfg is None else cfg
    resultado = povoar(banco, *alteracoes, cfg=cfg)
    graficos = {
        "produtividade": grafico_produtividade(resultado, cfg),
        "refugo": grafico_refugo(resultado, cfg),
        "otd": grafico_otd_mensal(resultado, cfg),
        "setup": grafico_setup(resultado, cfg),
    }
    return montar_html(
        resultado,
        cfg,
        graficos,
        ordens_atrasadas(banco, resultado, cfg.painel["top_atrasos"]),
        ultima_importacao(banco),
        gerado_em="08/09/2026 10:00",
    )


# ---------------------------------------------------------------------------
# Autocontido
# ---------------------------------------------------------------------------

def test_pagina_nao_referencia_nada_de_fora(banco):
    html = pagina(banco, {})
    externas = re.findall(r'(?:src|href)\s*=\s*["\'](?!data:)([^"\']+)', html)
    assert externas == []


def test_pagina_nao_tem_script_nem_import(banco):
    html = pagina(banco, {})
    assert "<script" not in html
    assert "@import" not in html
    assert "url(" not in html


def test_os_quatro_graficos_estao_embutidos(banco):
    html = pagina(banco, {})
    assert html.count("data:image/png;base64,") == 4


def test_todas_as_cores_do_config_entraram_no_css(banco):
    # O CSS e montado por substituicao de marcador. Um marcador sobrando sairia
    # como texto literal e a regra inteira seria descartada pelo navegador, em
    # silencio.
    html = pagina(banco, {})
    assert re.search(r"\{(fundo|cartao|verde|vermelho|texto|texto_fraco|borda)\}", html) is None
    assert "#29e07f" in html


# ---------------------------------------------------------------------------
# Escape do que vem da planilha
# ---------------------------------------------------------------------------

def test_texto_da_planilha_e_escapado_na_tabela(banco):
    html = pagina(
        banco,
        {
            "produto": "<script>alert(1)</script>",
            "data_entrega": date(2026, 3, 20),  # atrasada, para entrar na tabela
        },
    )
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_titulo_do_config_e_escapado(banco):
    cfg = cfg_teste(
        painel={**cfg_teste().painel, "titulo": "Painel <b>da</b> fábrica & cia"}
    )
    html = pagina(banco, {}, cfg=cfg)
    assert "<b>da</b>" not in html
    assert "&lt;b&gt;" in html


# ---------------------------------------------------------------------------
# Periodo incompleto
# ---------------------------------------------------------------------------

def test_nota_do_periodo_incompleto_aparece(banco):
    html = pagina(banco, {})
    assert "período incompleto" in html
    assert "otimista" in html


def test_grafico_de_otd_marca_o_mes_incompleto(banco):
    # A marcacao visual e do PNG e nao da para inspecionar aqui; o que da para
    # garantir e que o codigo entrou no caminho da marcacao, que e o mesmo que
    # produz a nota.
    resultado = povoar(banco, {})
    assert resultado.mes_incompleto == "2026-03"
    assert grafico_otd_mensal(resultado, cfg_teste()).startswith("data:image/png;base64,")


# ---------------------------------------------------------------------------
# Cartoes
# ---------------------------------------------------------------------------

def test_sao_cinco_cartoes(banco):
    resultado = povoar(banco, {})
    assert len(montar_cartoes(resultado, cfg_teste())) == 5


def test_cartao_de_contagem_diz_no_titulo_o_que_conta(banco):
    # Media de folhas/hora entre uma plana e uma digital nao tem meta que
    # signifique nada, entao o cartao conta maquinas. O titulo precisa dizer
    # isso: 'Produtividade' com o valor '1 de 4' obriga a ler o detalhe para
    # descobrir do que e a contagem.
    resultado = povoar(banco, {})
    cartao = montar_cartoes(resultado, cfg_teste())[0]
    assert cartao.titulo == "Máquinas na meta"
    assert cartao.valor == "1 de 1"


def test_titulo_da_pagina_sai_com_acento(banco):
    # A regra de escrever sem acentuacao e do terminal, por causa do console do
    # PowerShell, e nao da pagina.
    from src.config import carregar

    assert "ç" in carregar().painel["titulo"]


def test_indicador_sem_dado_nao_fica_verde_nem_vermelho(banco):
    # Uma ordem cancelada so: nada produzido, nada entregue.
    resultado = povoar(
        banco,
        {
            "status": STATUS_CANCELADA,
            "data_entrega": None,
            "tiragem_boa": 0,
            "refugo": 0,
            "tempo_producao_min": 0.0,
        },
    )
    cartoes = {c.titulo: c for c in montar_cartoes(resultado, cfg_teste())}
    assert cartoes["Refugo"].estado is None
    assert cartoes["Refugo"].valor == "sem dado"
    assert cartoes["Entrega no prazo"].estado is None


def test_cartao_sem_dado_sai_com_a_classe_neutra(banco):
    html = pagina(
        banco,
        {
            "status": STATUS_CANCELADA,
            "data_entrega": None,
            "tiragem_boa": 0,
            "refugo": 0,
            "tempo_producao_min": 0.0,
        },
    )
    assert 'class="cartao sem"' in html


def test_cartao_fora_da_meta_sai_com_a_classe_vermelha(banco):
    resultado = povoar(banco, {"refugo": 2000})  # muito acima dos 3%
    cartoes = {c.titulo: c for c in montar_cartoes(resultado, cfg_teste())}
    assert cartoes["Refugo"].estado is False


# ---------------------------------------------------------------------------
# Tabela de atrasos
# ---------------------------------------------------------------------------

def test_so_entram_as_ordens_entregues_fora_do_prazo(banco):
    resultado = povoar(
        banco,
        {},  # entrega 10/03, prazo 12/03: no prazo
        {"numero_os": "OS-2026-0002", "data_entrega": date(2026, 3, 20)},  # 8 dias
    )
    atrasos = ordens_atrasadas(banco, resultado, 10)
    assert [a.numero_os for a in atrasos] == ["OS-2026-0002"]
    assert atrasos[0].dias == 8


def test_atrasos_saem_do_maior_para_o_menor(banco):
    resultado = povoar(
        banco,
        {"numero_os": "OS-2026-0001", "data_entrega": date(2026, 3, 15)},
        {"numero_os": "OS-2026-0002", "data_entrega": date(2026, 3, 25)},
        {"numero_os": "OS-2026-0003", "data_entrega": date(2026, 3, 18)},
    )
    atrasos = ordens_atrasadas(banco, resultado, 10)
    assert [a.dias for a in atrasos] == [13, 6, 3]


def test_o_limite_do_config_e_respeitado(banco):
    resultado = povoar(
        banco,
        *[
            {"numero_os": f"OS-2026-{n:04d}", "data_entrega": date(2026, 3, 13 + n)}
            for n in range(1, 6)
        ],
    )
    assert len(ordens_atrasadas(banco, resultado, 2)) == 2


def test_ordem_nao_entregue_nunca_aparece_como_atraso(banco):
    resultado = povoar(
        banco, {"status": STATUS_EM_PRODUCAO, "data_entrega": None}
    )
    assert ordens_atrasadas(banco, resultado, 10) == []


def test_sem_atraso_a_tabela_diz_isso_em_vez_de_sumir(banco):
    html = pagina(banco, {})
    assert "Nenhuma ordem entregue fora do prazo" in html


# ---------------------------------------------------------------------------
# Rodape de qualidade
# ---------------------------------------------------------------------------

def test_rodape_mostra_a_taxa_de_rejeicao(banco):
    povoar(banco, {})
    registrar_execucao(
        banco, momento=MOMENTO, arquivos_lidos=3, linhas_lidas=736,
        linhas_gravadas=678, linhas_corrigidas=67, linhas_rejeitadas=42,
        duracao_segundos=1.0,
    )
    html = pagina(banco, {})
    assert "Qualidade dos dados" in html
    assert "736" in html
    assert "5,71%" in html


def test_banco_sem_importacao_registrada_diz_isso_em_vez_de_zerar(banco):
    # Zeros pareceriam uma importacao perfeita, que e o oposto da verdade.
    html = pagina(banco, {})
    assert "Não há registro de importação" in html


# ---------------------------------------------------------------------------
# Geracao do arquivo
# ---------------------------------------------------------------------------

def test_gerar_grava_o_arquivo(banco, tmp_path):
    povoar(banco, {})
    destino = tmp_path / "saida" / "painel.html"
    caminho, tamanho = gerar(
        cfg_teste(), banco=tmp_path / "producao.db", destino=destino
    )
    assert caminho == destino
    assert destino.is_file()
    # Um HTML pequeno demais significa grafico vazio: os quatro PNG em base64
    # sozinhos passam de 50 KB.
    assert tamanho > 50_000
    assert destino.read_text(encoding="utf-8").startswith("<!DOCTYPE html>")


def test_gerar_sem_banco_recusa(tmp_path):
    with pytest.raises(ErroPainel, match="banco nao encontrado"):
        gerar(cfg_teste(), banco=tmp_path / "nao_existe.db")


@pytest.mark.parametrize(
    "grafico",
    [grafico_produtividade, grafico_refugo, grafico_otd_mensal, grafico_setup],
)
def test_grafico_sem_dado_ainda_devolve_imagem(banco, grafico):
    # Espaco em branco no lugar do grafico pareceria defeito de carregamento.
    resultado = povoar(
        banco,
        {
            "status": STATUS_CANCELADA,
            "data_entrega": None,
            "tiragem_boa": 0,
            "refugo": 0,
            "tempo_producao_min": 0.0,
            "tempo_setup_min": 0.0,
        },
    )
    assert grafico(resultado, cfg_teste()).startswith("data:image/png;base64,")


# ---------------------------------------------------------------------------
# Formatacao
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Colocacao do rotulo das barras
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "altura, meta, colide, caso",
    [
        # Os numeros sao os do painel real, com o intervalo do eixo de cada um.
        (2.91, 3.0, True, "Komori encostando na meta de refugo"),
        (3.40, 3.0, False, "SM52 acima da meta: o tracejado fica abaixo"),
        (0.90, 3.0, False, "Versant longe da meta: cabe acima"),
    ],
)
def test_rotulo_vai_para_dentro_so_quando_a_meta_esta_logo_acima(
    altura, meta, colide, caso
):
    assert rotulo_acima_colide(altura, meta, 4.0) is colide, caso


@pytest.mark.parametrize(
    "altura, meta, colide",
    [
        (11037.0, 12000.0, True),   # Komori: meta 963 acima, dentro da folga
        (3997.0, 4500.0, True),     # Versant: meta 503 acima
        (9512.0, 9000.0, False),    # SM52: meta abaixo do topo
        (8727.0, 11000.0, False),   # SM74: meta 2273 acima, longe
    ],
)
def test_a_mesma_regra_serve_para_a_escala_de_folhas_por_hora(altura, meta, colide):
    assert rotulo_acima_colide(altura, meta, 14000.0) is colide


def test_barra_sem_meta_nunca_colide():
    assert rotulo_acima_colide(100.0, None, 200.0) is False


def test_eixo_sem_altura_nao_quebra_a_conta():
    # Guarda contra divisao implicita por um intervalo zerado quando o corte tem
    # uma barra so, com valor zero.
    assert rotulo_acima_colide(0.0, 3.0, 0.0) is False


@pytest.mark.parametrize(
    "valor, casas, esperado",
    [
        (1234.5, 1, "1.234,5"),
        (1234567.89, 2, "1.234.567,89"),
        (3.0, 2, "3,00"),
        (0.0, 1, "0,0"),
    ],
)
def test_numero_sai_no_padrao_brasileiro(valor, casas, esperado):
    assert _numero(valor, casas) == esperado


def test_numero_sem_valor_e_sem_dado():
    assert _numero(None) == "sem dado"
