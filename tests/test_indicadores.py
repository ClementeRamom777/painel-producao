"""
Testes dos indicadores.

Duas frentes. A primeira e aritmetica pura sobre Indicadores montado a mao: as
cinco formulas da secao 5, cada uma com um caso de numero redondo que da para
conferir de cabeca, e cada uma com o denominador zerado, que precisa devolver
None e nunca 'inf' nem 'NaN'.

A segunda e sobre um banco de verdade, pequeno, montado no tmp_path: e o que
prova que a consulta agrupa pelo campo certo, que os cortes somam de volta ao
total e que a ordem cancelada nao quebra nada.
"""

import csv
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from apoio import cfg_teste, linhas
from src.banco import conectar, criar_esquema, gravar_ordens
from src.indicadores import (
    GRUPO_TOTAL,
    SEM_VALOR,
    ErroIndicadores,
    Indicadores,
    ResultadoIndicadores,
    calcular,
    descrever_conferencia,
    exportar_conferencia,
    maquinas_na_meta,
    meta_de,
    na_meta,
    produtividade_na_meta,
)
from src.limpeza import STATUS_CANCELADA, STATUS_EM_PRODUCAO

MOMENTO = "2026-09-08T10:00:00"


def indicadores(**sobrepoe) -> Indicadores:
    """Um Indicadores com somas redondas, para a conta sair de cabeca."""
    campos = {
        "rotulo": "teste",
        "ordens": 10,
        "tiragem_boa": 12000,
        "refugo": 3000,
        "tempo_producao_min": 60.0,
        "tempo_setup_min": 40.0,
        "ordens_com_setup": 10,
        "entregues_com_prazo": 8,
        "entregues_no_prazo": 6,
        "entregues_sem_prazo": 0,
    }
    campos.update(sobrepoe)
    return Indicadores(**campos)


# ---------------------------------------------------------------------------
# As cinco formulas
# ---------------------------------------------------------------------------

def test_produtividade_e_folhas_boas_por_hora():
    # 12000 folhas em 60 minutos = 1 hora = 12000 folhas/h.
    assert indicadores().produtividade == 12000.0


def test_refugo_e_sobre_o_que_passou_pela_maquina():
    # 3000 / (12000 + 3000) = 20%. O denominador e boa + refugo, nao a tiragem
    # prevista: o percentual e do material que passou pela maquina.
    assert indicadores().refugo_pct == 20.0


def test_setup_medio_e_media_simples_por_ordem():
    # 40 minutos em 10 ordens = 4 minutos por job.
    assert indicadores().setup_medio_min == 4.0


def test_otd_e_sobre_as_entregues_com_prazo():
    assert indicadores().otd_pct == 75.0


def test_aproveitamento_e_producao_sobre_o_tempo_total():
    # 60 de producao contra 40 de acerto = 60%.
    assert indicadores().aproveitamento_pct == 60.0


# ---------------------------------------------------------------------------
# Divisao por zero: None, nunca inf nem NaN
# ---------------------------------------------------------------------------

def test_produtividade_sem_tempo_de_producao_e_none():
    # A ordem cancelada depois do acerto: o setup foi gasto, nada foi produzido.
    assert indicadores(tempo_producao_min=0.0).produtividade is None


def test_refugo_sem_nada_produzido_e_none():
    assert indicadores(tiragem_boa=0, refugo=0).refugo_pct is None


def test_setup_medio_sem_ordem_com_setup_e_none():
    assert indicadores(ordens_com_setup=0).setup_medio_min is None


def test_otd_sem_entrega_com_prazo_e_none():
    assert indicadores(entregues_com_prazo=0, entregues_no_prazo=0).otd_pct is None


def test_aproveitamento_sem_tempo_nenhum_e_none():
    vazio = indicadores(tempo_producao_min=0.0, tempo_setup_min=0.0)
    assert vazio.aproveitamento_pct is None


def test_nenhum_indicador_vira_inf_ou_nan():
    zerado = Indicadores(
        rotulo="cancelada", ordens=1, tiragem_boa=0, refugo=0,
        tempo_producao_min=0.0, tempo_setup_min=0.0, ordens_com_setup=0,
        entregues_com_prazo=0, entregues_no_prazo=0, entregues_sem_prazo=0,
    )
    for nome in (
        "produtividade", "refugo_pct", "setup_medio_min",
        "otd_pct", "aproveitamento_pct",
    ):
        assert getattr(zerado, nome) is None, nome


# ---------------------------------------------------------------------------
# Metas
# ---------------------------------------------------------------------------

def test_meta_de_maior_e_melhor():
    assert na_meta("otd_pct", 95.0, 95.0) is True
    assert na_meta("otd_pct", 94.9, 95.0) is False


def test_meta_de_menor_e_melhor():
    assert na_meta("refugo_pct", 3.0, 3.0) is True
    assert na_meta("refugo_pct", 3.1, 3.0) is False


def test_sem_dado_nao_e_nem_verde_nem_vermelho():
    # Pintar de verde o que nao foi medido e a forma mais facil de o painel
    # mentir.
    assert na_meta("otd_pct", None, 95.0) is None


def test_produtividade_nao_tem_meta_global():
    with pytest.raises(ErroIndicadores, match="metas_produtividade"):
        meta_de("produtividade", cfg_teste())


def test_produtividade_compara_com_a_meta_da_propria_maquina():
    cfg = cfg_teste(metas_produtividade={"Heidelberg SM52": 12000})
    assert produtividade_na_meta("Heidelberg SM52", indicadores(), cfg) is True

    cfg_exigente = cfg_teste(metas_produtividade={"Heidelberg SM52": 12001})
    assert produtividade_na_meta("Heidelberg SM52", indicadores(), cfg_exigente) is False


def test_maquina_sem_produtividade_medida_nao_entra_em_nenhum_lado():
    # Ela nao falhou a meta, ela nao foi medida. Contar como falha inventaria um
    # vermelho; contar como sucesso inventaria um verde.
    resultado = ResultadoIndicadores(
        total=indicadores(),
        por_maquina={
            "Heidelberg SM52": indicadores(),
            "Heidelberg SM74": indicadores(tempo_producao_min=0.0),
        },
    )
    cfg = cfg_teste(
        metas_produtividade={"Heidelberg SM52": 9000, "Heidelberg SM74": 9000}
    )
    dentro, medidas, pct = maquinas_na_meta(resultado, cfg)
    assert (dentro, medidas, pct) == (1, 1, 100.0)


# ---------------------------------------------------------------------------
# Sobre um banco de verdade
# ---------------------------------------------------------------------------

@pytest.fixture
def banco(tmp_path):
    """Banco vazio com o esquema pronto."""
    con = conectar(tmp_path / "producao.db")
    criar_esquema(con)
    yield con
    con.close()


def gravar(banco, *alteracoes, cfg=None):
    """Grava as linhas e devolve os indicadores calculados sobre elas."""
    cfg = cfg_teste() if cfg is None else cfg
    gravar_ordens(banco, linhas(*alteracoes), cfg, MOMENTO)
    return calcular(banco)


def test_banco_vazio_recusa(banco):
    with pytest.raises(ErroIndicadores, match="nenhuma ordem"):
        calcular(banco)


def test_total_soma_as_ordens(banco):
    resultado = gravar(
        banco,
        {},
        {"numero_os": "OS-2026-0002", "tiragem_boa": 900, "refugo": 100},
    )
    assert resultado.total.ordens == 2
    assert resultado.total.tiragem_boa == 5100 + 900
    assert resultado.total.refugo == 120 + 100
    assert resultado.total.rotulo == GRUPO_TOTAL


@pytest.mark.parametrize("corte", ["por_maquina", "por_turno", "por_mes"])
def test_cada_corte_soma_de_volta_ao_total(banco, corte):
    # Se um corte nao fecha com o total, alguma ordem sumiu do agrupamento - e
    # o painel mostraria duas contagens diferentes da mesma fabrica.
    resultado = gravar(
        banco,
        {},
        {"numero_os": "OS-2026-0002", "turno": "2"},
        {"numero_os": "OS-2026-0003", "data_entrada": date(2026, 4, 7),
         "data_prazo": date(2026, 4, 20),
         "data_entrega": date(2026, 4, 15)},
    )
    grupos = getattr(resultado, corte).values()
    assert sum(g.ordens for g in grupos) == resultado.total.ordens
    assert sum(g.tiragem_boa for g in grupos) == resultado.total.tiragem_boa


def test_agrupa_o_mes_pela_data_de_entrada(banco):
    # Entra em marco, sai em abril: conta em marco. E o que mantem a queda de
    # OTD no mes em que ela aconteceu, em vez de borrada entre dois.
    resultado = gravar(
        banco,
        {
            "data_entrada": date(2026, 3, 30),
            "data_prazo": date(2026, 4, 2),
            "data_entrega": date(2026, 4, 10),
        },
    )
    assert list(resultado.por_mes) == ["2026-03"]
    assert resultado.por_mes["2026-03"].otd_pct == 0.0


def test_ordem_sem_turno_aparece_como_sem_informacao(banco):
    # Turno e coluna opcional. Sem este rotulo a ordem sumiria do corte e a soma
    # dos turnos deixaria de bater com o total.
    resultado = gravar(banco, {"turno": None})
    assert list(resultado.por_turno) == [SEM_VALOR]
    assert resultado.por_turno[SEM_VALOR].ordens == resultado.total.ordens


def test_ordem_nao_entregue_fica_fora_do_otd(banco):
    resultado = gravar(
        banco,
        {},
        {
            "numero_os": "OS-2026-0002",
            "status": STATUS_EM_PRODUCAO,
            "data_entrega": None,
        },
    )
    assert resultado.total.ordens == 2
    assert resultado.total.entregues_com_prazo == 1


def test_ordem_cancelada_conta_no_setup_e_nao_quebra_a_produtividade(banco):
    # O acerto foi gasto e nada foi produzido: e tempo de maquina perdido de
    # verdade, e precisa aparecer no aproveitamento.
    resultado = gravar(
        banco,
        {
            "status": STATUS_CANCELADA,
            "data_entrega": None,
            "tiragem_boa": 0,
            "refugo": 0,
            "tempo_producao_min": 0.0,
            "tempo_setup_min": 25.0,
        },
    )
    assert resultado.total.tempo_setup_min == 25.0
    assert resultado.total.setup_medio_min == 25.0
    assert resultado.total.produtividade is None
    assert resultado.total.aproveitamento_pct == 0.0


def test_periodo_filtra_pela_coorte_de_entrada(banco):
    gravar_ordens(
        banco,
        linhas(
            {},
            {"numero_os": "OS-2026-0002", "data_entrada": date(2026, 5, 4),
             "data_prazo": date(2026, 5, 14), "data_entrega": date(2026, 5, 10)},
        ),
        cfg_teste(),
        MOMENTO,
    )
    resultado = calcular(banco, de="2026-05-01", ate="2026-05-31")
    assert resultado.total.ordens == 1
    assert list(resultado.por_mes) == ["2026-05"]


def test_periodo_invertido_recusa(banco):
    gravar(banco, {})
    with pytest.raises(ErroIndicadores, match="invertido"):
        calcular(banco, de="2026-06-01", ate="2026-01-01")


def test_ultimo_mes_do_banco_e_marcado_como_incompleto(banco):
    resultado = gravar(
        banco,
        {},
        {"numero_os": "OS-2026-0002", "data_entrada": date(2026, 5, 4),
         "data_prazo": date(2026, 5, 14), "data_entrega": date(2026, 5, 10)},
    )
    assert resultado.mes_incompleto == "2026-05"


def test_periodo_que_termina_antes_do_corte_nao_tem_mes_incompleto(banco):
    gravar_ordens(
        banco,
        linhas(
            {},
            {"numero_os": "OS-2026-0002", "data_entrada": date(2026, 5, 4),
             "data_prazo": date(2026, 5, 14), "data_entrega": date(2026, 5, 10)},
        ),
        cfg_teste(),
        MOMENTO,
    )
    # A censura a direita vem do corte da exportacao, nao do filtro. Marco nao
    # esta censurado so porque o usuario pediu um periodo mais curto.
    resultado = calcular(banco, de="2026-01-01", ate="2026-03-31")
    assert resultado.mes_incompleto is None


# ---------------------------------------------------------------------------
# Conferencia manual
# ---------------------------------------------------------------------------

def test_conferencia_mostra_as_somas_e_a_conta(banco):
    resultado = gravar(banco, {})
    texto = descrever_conferencia("Heidelberg SM52", resultado, cfg_teste())

    assert "SOMA(tiragem_boa)" in texto
    assert "5100" in texto
    assert "SOMA(refugo)" in texto
    assert "120" in texto
    # A conta armada com os mesmos numeros das somas.
    assert "5100 / (34.0 / 60)" in texto


def test_conferencia_de_maquina_sem_ordem_recusa(banco):
    resultado = gravar(banco, {})
    with pytest.raises(ErroIndicadores, match="Roland 700"):
        descrever_conferencia("Roland 700", resultado, cfg_teste())


def test_csv_de_conferencia_tem_as_ordens_somadas(banco, tmp_path):
    resultado = gravar(banco, {}, {"numero_os": "OS-2026-0002"})
    destino = tmp_path / "conferencia.csv"
    gravadas = exportar_conferencia(banco, "Heidelberg SM52", resultado, destino)

    assert gravadas == 2
    with destino.open(encoding="utf-8-sig", newline="") as arquivo:
        registros = list(csv.DictReader(arquivo, delimiter=";"))

    # O SOMA() do Excel sobre este arquivo tem que bater com o numero impresso.
    assert len(registros) == 2
    soma = sum(int(r["tiragem_boa"]) for r in registros)
    assert soma == resultado.por_maquina["Heidelberg SM52"].tiragem_boa


def test_somas_grandes_nao_perdem_digito_na_conferencia(banco):
    # O bloco existe para conferir na mao; um formato que arredonda um total de
    # seis digitos torna a conferencia impossivel de fechar.
    resultado = gravar(banco, {"tempo_producao_min": 123456.7, "tempo_setup_min": 0.0})
    texto = descrever_conferencia("Heidelberg SM52", resultado, cfg_teste())
    assert "123456.7" in texto
