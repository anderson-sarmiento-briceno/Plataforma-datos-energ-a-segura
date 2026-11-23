"""
Pruebas de data_classification.yaml y src/clasificacion.py (Paso 7, C1).

La prueba con base revisa que TODAS las columnas de la base estén clasificadas:
si alguien agrega una columna y no la clasifica, make test falla.
"""
import psycopg2
import pytest

from src.bd import conectar
from src.clasificacion import NIVEL_PII, cargar_clasificacion, columnas_a_cifrar
from src.config import cargar_config

CLASIFICACION = cargar_clasificacion()


def test_la_pii_del_cliente_se_cifra_desde_su_columna_de_origen() -> None:
    mapa = columnas_a_cifrar(CLASIFICACION, "core.dim_cliente")
    assert mapa["nit_cifrado"] == "nit"
    assert mapa["email_cifrado"] == "email_contacto"
    assert mapa["observaciones_cifrada"] == "observaciones"


def test_toda_columna_restringida_es_bytea_cifrada() -> None:
    # Una columna Restringido-PII siempre tiene "origen" (se cifra); ninguna queda en claro.
    for tabla, columnas in CLASIFICACION.items():
        for columna, regla in columnas.items():
            if regla["nivel"] == NIVEL_PII:
                assert regla["origen"], f"{tabla}.{columna} es PII pero no dice de dónde se cifra"


def test_la_vista_del_analista_no_tiene_pii() -> None:
    niveles = {r["nivel"] for r in CLASIFICACION["analytics.v_consumo_mensual"].values()}
    assert NIVEL_PII not in niveles


def test_un_nivel_mal_escrito_falla(tmp_path) -> None:
    ruta = tmp_path / "clasificacion.yaml"
    ruta.write_text("niveles: [Interno]\ntablas:\n  t:\n    c: Secretisimo\n")
    with pytest.raises(ValueError):
        cargar_clasificacion(ruta)


def test_todas_las_columnas_de_la_base_estan_clasificadas() -> None:
    try:
        cargar_config()
        # Superusuario: information_schema solo muestra las columnas que el rol
        # conectado puede ver, y aquí hay que revisarlas TODAS.
        conexion = conectar("superusuario")
    except (psycopg2.OperationalError, RuntimeError):
        pytest.skip("Postgres no está arriba (correr make up)")
    try:
        with conexion.cursor() as cursor:
            cursor.execute("""
                SELECT table_schema || '.' || table_name, column_name
                FROM information_schema.columns
                WHERE table_schema IN ('core', 'cuarentena', 'control', 'audit', 'analytics')""")
            en_la_base = cursor.fetchall()
    finally:
        conexion.close()
    sin_clasificar = [f"{t}.{c}" for t, c in en_la_base if c not in CLASIFICACION.get(t, {})]
    assert en_la_base and not sin_clasificar, f"Columnas sin clasificar: {sin_clasificar}"
