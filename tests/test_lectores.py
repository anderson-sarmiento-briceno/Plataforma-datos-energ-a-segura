"""
Pruebas de src/lectores.py (Paso 4).

Cada prueba arma un CSV pequeño inventado en tmp_path (carpeta temporal que
pytest crea y borra): no usan data/ ni Docker. Una prueba por cada formato o
trampa que encontró la exploración, más los casos del README (archivo truncado).
"""
from decimal import Decimal

import pandas as pd
import pytest

from src.lectores import (ArchivoInvalido, convertir_valor, leer_archivo_consumo,
                          listar_archivos_consumo)

ENCABEZADO = "frontera_id,timestamp,consumo_kwh,version_lectura,calidad_lectura"


def escribir(tmp_path, nombre, texto, encoding="utf-8"):
    """Crea un archivo de prueba con saltos de línea de Windows (CRLF), como los reales."""
    ruta = tmp_path / nombre
    ruta.write_bytes(texto.replace("\n", "\r\n").encode(encoding))
    return ruta


def bogota(texto):
    """Instante esperado en hora de Bogotá, para comparar."""
    return pd.Timestamp(texto, tz="America/Bogota")


# ----------------------------- formatos de los meses -----------------------------

def test_formato_enero_hora_local_y_punto_decimal(tmp_path) -> None:
    ruta = escribir(tmp_path, "consumos_2026-01.csv",
                    f"{ENCABEZADO}\nCO1,2026-01-01 00:00:00,275.283,1,OK\n")
    df = leer_archivo_consumo(ruta)
    assert df.loc[0, "ts_medicion"] == bogota("2026-01-01 00:00:00")
    assert df.loc[0, "kwh"] == Decimal("275.283")      # Decimal exacto, no float
    assert df.loc[0, "version_lectura"] == 1
    assert df.loc[0, "fila_origen"] == 1


def test_formato_febrero_punto_y_coma_coma_decimal_y_utc(tmp_path) -> None:
    ruta = escribir(tmp_path, "consumos_2026-02.csv",
                    "frontera_id;timestamp_utc;consumo_kwh;version_lectura;calidad_lectura\n"
                    "CO1;2026-02-01T05:00:00Z;163,795;1;OK\n")
    df = leer_archivo_consumo(ruta)
    # 05:00 UTC = 00:00 en Bogotá: la primera hora de febrero sigue en febrero (B1)
    assert df.loc[0, "ts_medicion"] == bogota("2026-02-01 00:00:00")
    assert df.loc[0, "kwh"] == Decimal("163.795")


def test_formato_marzo_columna_kwh_y_mwh_a_kwh(tmp_path) -> None:
    ruta = escribir(tmp_path, "consumos_2026-03.csv",
                    "frontera_id,timestamp,kwh,unidad,version_lectura,calidad_lectura\n"
                    "CO1,2026-03-01 00:00:00,126.985,kWh,1,OK\n"
                    "CO1,2026-03-01 01:00:00,0.4,MWh,1,OK\n")
    df = leer_archivo_consumo(ruta)
    assert df.loc[0, "kwh"] == Decimal("126.985")
    assert df.loc[1, "kwh"] == Decimal("400")          # 0.4 MWh × 1000, exacto
    assert df.loc[1, "unidad"] == "MWh"                # se conserva para contar conversiones


def test_archivo_latin1_y_con_bom(tmp_path) -> None:
    latin = escribir(tmp_path, "a.csv", f"{ENCABEZADO}\nCO1,2026-01-01 00:00:00,1.5,1,OK á\n", "latin-1")
    bom = escribir(tmp_path, "b.csv", f"{ENCABEZADO}\nCO1,2026-01-01 00:00:00,1.5,1,OK\n", "utf-8-sig")
    assert leer_archivo_consumo(latin).loc[0, "calidad_lectura"] == "OK á"
    assert leer_archivo_consumo(bom).loc[0, "frontera_id"] == "CO1"   # el BOM no daña la columna


# ----------------------------- valores raros: la fila NO se pierde -----------------------------

def test_valores_raros_salen_como_none_sin_perder_la_fila(tmp_path) -> None:
    ruta = escribir(tmp_path, "c.csv", f"{ENCABEZADO}\n"
                    "CO1,2026-01-01 00:00:00,,1,OK\n"            # vacío
                    "CO1,2026-01-01 01:00:00,abc,1,OK\n"         # no numérico
                    "CO1,2026-01-01 02:00:00,NaN,1,OK\n"         # Decimal lo aceptaría
                    "CO1,fecha mala,10,1,OK\n"                   # hora ilegible
                    "CO1,2026-01-01 04:00:00,-5,1,OK\n")         # negativo: lo juzga el Paso 5
    df = leer_archivo_consumo(ruta)
    assert len(df) == 5                                          # ninguna fila descartada
    assert df["kwh"].tolist()[:3] == [None, None, None]
    assert pd.isna(df.loc[3, "ts_medicion"])
    assert df.loc[4, "kwh"] == Decimal("-5")
    assert df.loc[1, "valor_texto"] == "abc"                     # el texto original se conserva


def test_unidad_desconocida_no_se_adivina() -> None:
    assert convertir_valor("10", "GWh") is None
    assert convertir_valor("10", " mwh ") == Decimal("10000")   # mayúsculas y espacios no importan


# ----------------------------- archivos rotos: se rechazan enteros -----------------------------

def test_archivo_sin_columna_de_valor_se_rechaza(tmp_path) -> None:
    ruta = escribir(tmp_path, "d.csv", "frontera_id,timestamp,version_lectura\nCO1,2026-01-01 00:00:00,1\n")
    with pytest.raises(ArchivoInvalido) as error:
        leer_archivo_consumo(ruta)
    assert error.value.motivo == "COLUMNAS_FALTANTES"


def test_archivo_truncado_se_rechaza(tmp_path) -> None:
    # La última fila quedó cortada a la mitad: tiene menos campos que el encabezado.
    ruta = escribir(tmp_path, "e.csv", f"{ENCABEZADO}\nCO1,2026-01-01 00:00:00,1.5,1,OK\nCO1,2026-01-01 01:")
    with pytest.raises(ArchivoInvalido) as error:
        leer_archivo_consumo(ruta)
    assert error.value.motivo == "FILAS_INCOMPLETAS"


def test_archivo_con_campos_de_mas_se_rechaza(tmp_path) -> None:
    # pd.read_csv correría las columnas en silencio; el lector debe rechazarlo.
    ruta = escribir(tmp_path, "f.csv", f"{ENCABEZADO}\nCO1,2026-01-01 00:00:00,1.5,1,OK,x,y\n")
    with pytest.raises(ArchivoInvalido) as error:
        leer_archivo_consumo(ruta)
    assert error.value.motivo == "FILAS_CON_CAMPOS_DE_MAS"


def test_comillas_y_salto_de_linea_dentro_de_un_campo_no_rompen(tmp_path) -> None:
    # El parser csv respeta comillas: una coma o un salto dentro de "..." es parte del campo.
    ruta = escribir(tmp_path, "i.csv", f'{ENCABEZADO}\nCO1,2026-01-01 00:00:00,1.5,1,"OK, revisada\nmanual"\n')
    df = leer_archivo_consumo(ruta)
    assert len(df) == 1
    assert df.loc[0, "calidad_lectura"].startswith("OK, revisada")


def test_archivo_vacio_o_basura_se_rechaza(tmp_path) -> None:
    vacio = escribir(tmp_path, "g.csv", "")
    basura = tmp_path / "h.csv"
    basura.write_bytes(bytes(range(256)) * 4)                     # bytes sin sentido
    for ruta in (vacio, basura):
        with pytest.raises(ArchivoInvalido):
            leer_archivo_consumo(ruta)


# ----------------------------- descubrimiento de archivos -----------------------------

def test_listar_en_orden_lexicografico_y_solo_csv(tmp_path) -> None:
    for nombre in ("consumos_2026-04_correccion.csv", "consumos_2026-04.csv",
                   "consumos_2026-01.csv", "notas.txt"):
        (tmp_path / nombre).write_text("x")
    nombres = [p.split("/")[-1] for p in listar_archivos_consumo(tmp_path)]
    assert nombres == ["consumos_2026-01.csv", "consumos_2026-04.csv",
                       "consumos_2026-04_correccion.csv"]
