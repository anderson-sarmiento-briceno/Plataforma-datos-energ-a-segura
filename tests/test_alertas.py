"""
Pruebas de src/alertas.py (Paso 9). Mediciones INVENTADAS, sin Kafka:
las reglas son funciones puras, así que se prueban con listas pequeñas.

Cada prueba cubre un caso borde que el archivo real podría no traer.
"""
from decimal import Decimal

from src.alertas import INICIO_REJILLA, PASO, calcular_alertas, validar_medicion

LIMITES = {"CO1": Decimal("200"), "CO2": Decimal("50")}


def m(frontera, intervalo, kwh=10):
    """Una medición de `frontera` en el intervalo número `intervalo` de la rejilla (0 = 00:00)."""
    return {"frontera_id": frontera, "ts": INICIO_REJILLA + PASO * intervalo,
            "kwh": Decimal(str(kwh)) if kwh is not None else None}


def dia_completo(frontera, excepto=()):
    """Las 96 mediciones del día de una frontera, menos los intervalos de `excepto`."""
    return [m(frontera, i) for i in range(96) if i not in excepto]


def tipos(alertas, tipo):
    return [(a["frontera_id"], a["ts"]) for a in alertas if a["tipo"] == tipo]


# ----------------------------- CONSUMO_FUERA_DE_BANDA -----------------------------

def test_fuera_de_banda_es_estrictamente_mayor() -> None:
    mediciones = dia_completo("CO1") + dia_completo("CO2")
    mediciones[0]["kwh"] = Decimal("200")          # igual al máximo → NO alerta
    mediciones[1]["kwh"] = Decimal("200.001")      # mayor → alerta
    alertas = calcular_alertas(mediciones, LIMITES, ["CO1", "CO2"])
    assert tipos(alertas, "CONSUMO_FUERA_DE_BANDA") == [("CO1", "2026-06-01T00:15:00-05:00")]


# ----------------------------- LECTURA_RETROACTIVA -----------------------------

def test_retroactiva_y_la_atrasada_no_cambia_la_ultima() -> None:
    # Llegan 10:15 (41), luego 10:00 (40, atrasada) y luego 10:30 (42, normal).
    mediciones = [m("CO1", 41), m("CO1", 40), m("CO1", 42)]
    alertas = calcular_alertas(mediciones, LIMITES, [])
    assert tipos(alertas, "LECTURA_RETROACTIVA") == [("CO1", "2026-06-01T10:00:00-05:00")]


def test_misma_hora_repetida_no_es_retroactiva() -> None:
    alertas = calcular_alertas([m("CO1", 5), m("CO1", 5)], LIMITES, [])
    assert tipos(alertas, "LECTURA_RETROACTIVA") == []


def test_retroactiva_es_por_frontera() -> None:
    # CO2 llega "antes" en el tiempo que CO1, pero es otra frontera: no es atrasada.
    alertas = calcular_alertas([m("CO1", 41), m("CO2", 40)], LIMITES, [])
    assert tipos(alertas, "LECTURA_RETROACTIVA") == []


# ----------------------------- FRONTERA_SIN_REPORTE -----------------------------

def test_tres_vacios_dan_una_alerta_en_el_tercero() -> None:
    alertas = calcular_alertas(dia_completo("CO1", excepto={10, 11, 12}), LIMITES, ["CO1"])
    assert tipos(alertas, "FRONTERA_SIN_REPORTE") == [("CO1", "2026-06-01T03:00:00-05:00")]


def test_dos_vacios_no_alertan_y_cinco_dan_una_sola() -> None:
    dos = calcular_alertas(dia_completo("CO1", excepto={10, 11}), LIMITES, ["CO1"])
    cinco = calcular_alertas(dia_completo("CO1", excepto={10, 11, 12, 13, 14}), LIMITES, ["CO1"])
    assert tipos(dos, "FRONTERA_SIN_REPORTE") == []
    assert len(tipos(cinco, "FRONTERA_SIN_REPORTE")) == 1


def test_cae_vuelve_y_cae_de_nuevo_da_dos_alertas() -> None:
    alertas = calcular_alertas(dia_completo("CO1", excepto={10, 11, 12, 20, 21, 22}), LIMITES, ["CO1"])
    assert len(tipos(alertas, "FRONTERA_SIN_REPORTE")) == 2


def test_frontera_que_nunca_reporto_alerta_una_vez() -> None:
    alertas = calcular_alertas(dia_completo("CO1"), LIMITES, ["CO1", "CO2"])
    assert tipos(alertas, "FRONTERA_SIN_REPORTE") == [("CO2", "2026-06-01T00:30:00-05:00")]


# ----------------------------- formato y robustez -----------------------------

def test_sin_duplicados_y_ts_con_offset() -> None:
    mediciones = dia_completo("CO1")
    mediciones[3]["kwh"] = Decimal("999")
    # La misma medición llega 2 veces SEGUIDAS (si la repetida llegara al final del día,
    # sería además retroactiva: llegaría después de las 23:45 con hora 00:45).
    mediciones.insert(4, dict(mediciones[3]))
    alertas = calcular_alertas(mediciones, LIMITES, ["CO1"])
    assert len(alertas) == 1
    assert alertas[0] == {"tipo": "CONSUMO_FUERA_DE_BANDA", "frontera_id": "CO1",
                          "ts": "2026-06-01T00:45:00-05:00"}


def test_validar_medicion() -> None:
    buena = validar_medicion({"frontera_id": "CO1", "ts_medicion": "2026-06-01T05:00:00Z", "kwh": 9.122})
    assert buena["ts"] == INICIO_REJILLA and buena["kwh"] == Decimal("9.122")   # 05:00 UTC = 00:00 Bogotá
    assert validar_medicion({"frontera_id": "CO1", "ts_medicion": "2026-06-01 00:00:00", "kwh": 1}) is None
    assert validar_medicion({"ts_medicion": "2026-06-01T00:00:00-05:00", "kwh": 1}) is None
    assert validar_medicion("no soy un diccionario") is None
    sin_kwh = validar_medicion({"frontera_id": "CO1", "ts_medicion": "2026-06-01T00:00:00-05:00", "kwh": None})
    assert sin_kwh is not None and sin_kwh["kwh"] is None    # cuenta como "reportó", sin banda
