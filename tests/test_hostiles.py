"""
Pruebas de B7 (Paso 6): entradas hostiles en el maestro de clientes.

[README B7] Comillas anidadas, un campo citado con un salto de línea adentro,
cadenas larguísimas y algo que parece SQL: son datos, no código, y ninguna
fila puede perderse en silencio. Sobre todo, la tabla de consumos no puede
desaparecer ni quedar vacía al procesar el maestro de clientes.

Se usan clientes INVENTADOS (TEST_H*), sin datos personales: las columnas de
PII van vacías. No se depende de los datos reales.

La prueba con base de datos se SALTA si Postgres no está arriba. En la
secuencia normal (setup, up, run, stream, test) la base sí está arriba
durante `make test`, así que ahí corre de verdad.
"""
import csv
import os

import psycopg2
import pytest

from src.bd import conectar
from src.clasificacion import cargar_clasificacion
from src.config import cargar_config
from src.maestros import cargar_clientes, leer_csv_maestro

# Llaves de prueba, al azar: la prueba no usa ni necesita las llaves reales de .env.
SEGURIDAD = {"llave": os.urandom(32), "llave_hmac": os.urandom(32),
             "clasificacion": cargar_clasificacion()}

COLUMNAS = ["cliente_id", "razon_social", "nit", "email_contacto", "celular_contacto",
            "direccion", "ciudad", "departamento", "representante_legal", "observaciones"]

# Las 4 trampas que anuncia B7, una por cliente.
HOSTILES = {
    "TEST_H1": 'Comillas "anidadas" y, además, una coma S.A.S.',
    "TEST_H2": "Razón social con salto\nde línea adentro S.A.S.",
    "TEST_H3": "Larguísima " * 200,                                    # 2.200 caracteres
    "TEST_H4": "Prueba'); DROP TABLE core.fact_consumo_horario;-- S.A.S.",
}


def escribir_clientes(carpeta, con_fila_sin_id=False):
    """Escribe un clientes.csv inventado en Latin-1 (como el real), con las 4 trampas.

    csv.writer pone las comillas igual que el archivo real: duplica las comillas
    internas ("") y encierra entre comillas los campos con coma o salto de línea.
    """
    ruta = carpeta / "clientes.csv"
    with open(ruta, "w", encoding="latin-1", newline="") as f:
        escritor = csv.writer(f, lineterminator="\r\n")
        escritor.writerow(COLUMNAS)
        for cliente_id, razon in HOSTILES.items():
            escritor.writerow([cliente_id, razon, "", "", "", "", "Bogotá", "Cundinamarca", "", ""])
        if con_fila_sin_id:
            escritor.writerow(["", "Fila sin id", "", "", "", "", "Cali", "Valle", "", ""])
    return ruta


def test_las_4_trampas_se_leen_completas_y_sin_cambios(tmp_path) -> None:
    filas = leer_csv_maestro(escribir_clientes(tmp_path))
    assert len(filas) == 4                                  # 4 entran, 4 salen (no 5 por el salto de línea)
    for fila in filas:
        assert fila["razon_social"] == HOSTILES[fila["cliente_id"]]   # idénticas, sin "limpiar"
    assert filas[0]["ciudad"] == "Bogotá"                  # Latin-1 leído bien


@pytest.fixture
def conexion():
    """Conexión real a Postgres, o se salta la prueba si la base no está arriba."""
    try:
        cargar_config()
        conexion = conectar()
    except (psycopg2.OperationalError, RuntimeError):
        pytest.skip("Postgres no está arriba (correr make up)")
    yield conexion
    conexion.rollback()   # nada de la prueba queda guardado
    conexion.close()


def test_la_inyeccion_no_se_ejecuta_y_ningun_cliente_se_pierde(tmp_path, conexion) -> None:
    escribir_clientes(tmp_path, con_fila_sin_id=True)
    with conexion.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM core.fact_consumo_horario")
        filas_antes = cursor.fetchone()[0]

        cargados, rechazados = cargar_clientes(cursor, tmp_path, 100, SEGURIDAD)

        # B7: 5 leídos = 4 cargados + 1 en cuarentena. Ninguno se pierde en silencio.
        assert (cargados, rechazados) == (4, 1)
        # Lo crítico: la tabla de consumos sigue existiendo y con las mismas filas.
        cursor.execute("SELECT to_regclass('core.fact_consumo_horario') IS NOT NULL")
        assert cursor.fetchone()[0] is True
        cursor.execute("SELECT count(*) FROM core.fact_consumo_horario")
        assert cursor.fetchone()[0] == filas_antes
        # "Son datos, no código": cada texto quedó guardado tal cual.
        cursor.execute("SELECT cliente_id, razon_social FROM core.dim_cliente WHERE cliente_id LIKE 'TEST_H%%'")
        assert dict(cursor.fetchall()) == HOSTILES
