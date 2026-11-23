"""
Pruebas de src/calidad.py (Paso 5): reglas B3 y B4 como funciones puras.

No usan base de datos: cada prueba arma unas pocas lecturas con el mismo
formato que entrega el lector (Paso 4) y revisa qué decide la regla.
"""
from decimal import Decimal

import pandas as pd

from src.calidad import (gana_nueva, motivo_invalido, resolver_contra_existentes,
                         resolver_duplicados, separar_invalidos)
from src.lectores import convertir_valor

FRONTERAS = {"CO1", "CO2"}


def lectura(valor="10.5", hora="2026-04-01 00:00:00", frontera="CO1", version=1, fila=1,
            archivo="consumos_2026-04.csv", unidad="kWh", calidad="OK"):
    """Una lectura con el mismo formato que devuelve leer_archivo_consumo."""
    return {"frontera_id": frontera,
            "ts_medicion": pd.Timestamp(hora, tz="America/Bogota") if hora else pd.NaT,
            "kwh": convertir_valor(valor, unidad), "version_lectura": version,
            "calidad_lectura": calidad, "unidad": unidad, "ts_texto": hora or "",
            "valor_texto": valor, "version_texto": str(version),
            "archivo_origen": archivo, "fila_origen": fila}


def tabla(*lecturas):
    return pd.DataFrame(list(lecturas))


def motivo(**kwargs):
    """Motivo de invalidez de una sola lectura."""
    return motivo_invalido(next(tabla(lectura(**kwargs)).itertuples()), FRONTERAS)


# ----------------------------- B4: cada motivo -----------------------------

def test_motivos_de_invalidez() -> None:
    assert motivo() is None                                   # lectura válida
    assert motivo(valor="") == "NULO"
    assert motivo(valor="abc") == "NO_NUMERICO"
    assert motivo(valor="NaN") == "NO_NUMERICO"
    assert motivo(valor="5", unidad="GWh") == "UNIDAD_DESCONOCIDA"
    assert motivo(valor="-0.001") == "NEGATIVO"
    assert motivo(valor="1000000") is None                    # el límite exacto es válido (">", no ">=")
    assert motivo(valor="1000000.001") == "OUTLIER"
    assert motivo(valor="1001", unidad="MWh") == "OUTLIER"    # 1.001.000 kWh: se convierte ANTES de comparar
    assert motivo(hora=None) == "HORA_INVALIDA"
    assert motivo(frontera="CO_NO_EXISTE") == "FRONTERA_DESCONOCIDA"


def test_separar_invalidos_no_pierde_filas() -> None:
    df = tabla(lectura(fila=1), lectura(valor="", fila=2), lectura(valor="-1", fila=3))
    validas, rechazadas = separar_invalidos(df, FRONTERAS)
    assert len(validas) + len(rechazadas) == 3
    assert rechazadas["motivo"].tolist() == ["NULO", "NEGATIVO"]


# ----------------------------- B3 dentro de un archivo -----------------------------

def test_duplicados_exactos_y_conflictivos_se_cuentan_por_fila() -> None:
    # Grupo A, A, B de la misma hora: gana la última (B); sobran 2 filas.
    df = tabla(lectura(valor="10", fila=1), lectura(valor="10", fila=2), lectura(valor="12", fila=3))
    ganadoras, sobrantes = resolver_duplicados(df)
    assert ganadoras["kwh"].tolist() == [Decimal("12")]
    assert sorted(sobrantes["motivo"]) == ["DUPLICADO_CONFLICTIVO", "DUPLICADO_EXACTO"]


def test_gana_la_version_mayor_aunque_este_antes_en_el_archivo() -> None:
    df = tabla(lectura(valor="99", version=2, fila=1), lectura(valor="10", version=1, fila=2))
    ganadoras, _ = resolver_duplicados(df)
    assert ganadoras["kwh"].tolist() == [Decimal("99")]


def test_una_lectura_invalida_no_compite() -> None:
    # ADR-004: B4 antes que B3. La v2 negativa va a cuarentena y la v1 válida se conserva.
    df = tabla(lectura(valor="10", version=1, fila=1), lectura(valor="-5", version=2, fila=2))
    validas, rechazadas = separar_invalidos(df, FRONTERAS)
    ganadoras, sobrantes = resolver_duplicados(validas)
    assert ganadoras["kwh"].tolist() == [Decimal("10")]
    assert rechazadas["motivo"].tolist() == ["NEGATIVO"] and sobrantes.empty


# ----------------------------- B3 entre archivos (correcciones) -----------------------------

def test_desempate_entre_archivos() -> None:
    abril, correccion = "consumos_2026-04.csv", "consumos_2026-04_correccion.csv"
    assert gana_nueva((2, correccion, 1), (1, abril, 500))      # versión mayor gana
    assert not gana_nueva((1, abril, 500), (2, correccion, 1))  # versión menor NO pisa a la mayor
    assert gana_nueva((1, correccion, 1), (1, abril, 500))      # empate: archivo posterior ('_' > '.')
    assert gana_nueva((1, abril, 7), (1, abril, 7))             # misma lectura reprocesada: se reescribe igual


def test_correccion_reemplaza_y_se_cuenta() -> None:
    nueva = tabla(lectura(valor="480.481", version=2, archivo="consumos_2026-04_correccion.csv"))
    existentes = tabla(lectura(valor="10", version=1, archivo="consumos_2026-04.csv", fila=50))
    a_cargar, superadas, correcciones = resolver_contra_existentes(nueva, existentes)
    assert len(a_cargar) == 1 and superadas.empty and correcciones == 1


def test_lectura_vieja_no_pisa_una_correccion_ya_cargada() -> None:
    vieja = tabla(lectura(valor="10", version=1, archivo="consumos_2026-04.csv", fila=50))
    existentes = tabla(lectura(valor="480.481", version=2, archivo="consumos_2026-04_correccion.csv"))
    a_cargar, superadas, correcciones = resolver_contra_existentes(vieja, existentes)
    assert a_cargar.empty and correcciones == 0
    assert superadas["motivo"].tolist() == ["SUPERADA_POR_VERSION_MAYOR"]


def test_perder_por_empate_tiene_su_propio_motivo() -> None:
    # Misma versión: la ya cargada viene de un archivo posterior, así que gana ella.
    vieja = tabla(lectura(version=1, archivo="consumos_2026-04.csv", fila=50))
    existentes = tabla(lectura(version=1, archivo="consumos_2026-04_correccion.csv"))
    _, superadas, _ = resolver_contra_existentes(vieja, existentes)
    assert superadas["motivo"].tolist() == ["SUPERADA_POR_DESEMPATE"]


def test_misma_hora_escrita_en_utc_se_reconoce_como_existente() -> None:
    # 2026-04-01 00:00 en Bogotá == 2026-04-01 05:00 UTC: es la misma lectura.
    nueva = tabla(lectura(version=2, archivo="b.csv"))
    existentes = tabla(lectura(version=1, archivo="a.csv"))
    existentes["ts_medicion"] = existentes["ts_medicion"].dt.tz_convert("UTC")
    _, _, correcciones = resolver_contra_existentes(nueva, existentes)
    assert correcciones == 1
