"""
Pruebas de src/salidas.py y src/logs.py (Paso 8).
"""
import json
import logging
from decimal import Decimal

import psycopg2
import pytest

from src.bd import conectar
from src.config import cargar_config
from src.logs import FormatoJson
from src.salidas import armar_dq_report, calcular_huecos, escribir_consumo_mensual, horas_del_mes

# [README] Las 12 llaves obligatorias de dq_report.json.
LLAVES_DQ = {"filas_leidas", "duplicados_exactos", "duplicados_conflictivos", "registros_negativos",
             "registros_nulos", "registros_outlier", "registros_mwh_convertidos",
             "correcciones_tardias_aplicadas", "horas_sin_lectura_valida_total",
             "horas_sin_lectura_valida_por_frontera", "fronteras_huerfanas_facturacion",
             "clientes_con_pii_en_texto_libre"}


def test_horas_del_mes_en_bogota() -> None:
    assert [horas_del_mes(m) for m in ("2026-01", "2026-02", "2026-03", "2026-04")] == [744, 672, 744, 720]
    assert horas_del_mes("2028-02") == 696                     # año bisiesto


def test_huecos_incluyen_la_frontera_que_no_reporto_en_un_mes() -> None:
    # CO1 tiene 740 de 744 horas en enero y 672 de 672 en febrero; CO2 no reportó en febrero.
    filas = [("CO1", "2026-01", Decimal("1"), 740), ("CO1", "2026-02", Decimal("1"), 672),
             ("CO2", "2026-01", Decimal("1"), 744)]
    huecos = calcular_huecos(filas, ["CO1", "CO2", "CO3"], ["2026-01", "2026-02"])
    assert huecos == {"CO1": 4, "CO2": 672, "CO3": 744 + 672}   # B5: hueco = no existía o se descartó


def test_csv_con_3_decimales_exactos(tmp_path) -> None:
    ruta = tmp_path / "consumo_mensual.csv"
    escribir_consumo_mensual([("CO1", "2026-01", Decimal("183421.5534"), 744),
                              ("CO1", "2026-02", Decimal("400"), 672)], ruta)
    assert ruta.read_text().splitlines() == ["frontera_id,mes,consumo_kwh,horas_con_dato",
                                             "CO1,2026-01,183421.553,744", "CO1,2026-02,400.000,672"]


def test_log_en_json_y_sin_pii() -> None:
    # Capa 2: aunque alguien registrara un dato personal por error, sale tachado.
    registro = logging.LogRecord("prueba", logging.INFO, __file__, 1, "evento_de_prueba", None, None)
    registro.datos = {"archivo": "consumos_2026-01.csv", "filas": 10,
                      "descuidado": "contacto juan.perez@empresa.com tel 3001234567 CC 12345678"}
    linea = FormatoJson().format(registro)
    evento = json.loads(linea)                               # sigue siendo JSON válido
    assert evento["evento"] == "evento_de_prueba" and evento["filas"] == 10
    assert "juan.perez" not in linea and "3001234567" not in linea and "12345678" not in linea
    assert "consumos_2026-01.csv" in linea                   # lo que no es PII no se toca


def test_dq_report_tiene_las_12_llaves() -> None:
    try:
        cargar_config()
        conexion = conectar("ingesta")
    except (psycopg2.OperationalError, RuntimeError):
        pytest.skip("Postgres no está arriba (correr make up)")
    try:
        with conexion.cursor() as cursor:
            reporte = armar_dq_report(cursor, [], clientes_con_pii=0)
    finally:
        conexion.rollback()
        conexion.close()
    assert set(reporte) == LLAVES_DQ
