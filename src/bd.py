"""
Conexión a Postgres y helpers. Regla: TODO SQL con parámetros (%s).

Es el mismo patrón que sqlite3 con "?" en Detector-de-placas/src/database.py:
el dato viaja separado de la orden SQL, así que un texto como
"'); DROP TABLE ...;--" se guarda como texto y nunca se ejecuta.
"""
import hashlib
import os

import psycopg2
from psycopg2.extras import execute_values


def variable(nombre):
    """Lee una variable de entorno obligatoria; falla con un mensaje claro si no está."""
    valor = os.getenv(nombre)
    if not valor:
        raise RuntimeError(f"Falta la variable {nombre} en .env (correr make setup)")
    return valor


# Variables de .env (usuario, contraseña) de cada rol.
CREDENCIALES = {
    "ingesta": ("PG_ROL_INGESTA_USER", "PG_ROL_INGESTA_PASSWORD"),
    "analista": ("PG_ROL_ANALISTA_USER", "PG_ROL_ANALISTA_PASSWORD"),
    "auditor": ("PG_ROL_AUDITOR_USER", "PG_ROL_AUDITOR_PASSWORD"),
    "superusuario": ("PG_SUPERUSER", "PG_SUPERPASSWORD"),
}


def conectar(rol="ingesta"):
    """Abre una conexión con las credenciales de .env del rol indicado.

    [README C4] El pipeline se conecta como `ingesta`, NO como superusuario:
    si algo sale mal (un error, una inyección), el daño queda limitado a lo que
    ingesta puede hacer (no puede borrar tablas ni tocar la bitácora).
    Los demás roles los usan las pruebas de permisos.
    autocommit queda en False: cada archivo se carga en UNA transacción y se
    confirma (commit) o deshace (rollback) completo.
    """
    usuario, clave = CREDENCIALES[rol]
    return psycopg2.connect(
        host=variable("PG_HOST"),
        port=variable("PG_PORT"),
        dbname=variable("PG_DATABASE"),
        user=variable(usuario),
        password=variable(clave),
    )


def insertar_lote(cursor, sql, filas, tamano_lote):
    """Inserta muchas filas en pocos INSERT, sin dejar de usar parámetros.

    execute_values arma "INSERT ... VALUES (...), (...), ..." con los valores
    escapados por psycopg2. Fila por fila serían más de 115.000 INSERT (lento
    para que una corrida tarde segundos, no minutos); por lotes es igual de seguro.
    """
    if filas:
        execute_values(cursor, sql, filas, page_size=tamano_lote)


def hash_archivo(ruta):
    """Huella SHA-256 del contenido: si un byte cambia, la huella cambia."""
    with open(ruta, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def ya_procesado(cursor, archivo, huella):
    """¿Este archivo, con este mismo contenido, ya se cargó o ya se rechazó?

    Es la carga incremental (como ETL-TablaControl.py, pero por hash en vez de
    fechas): la segunda corrida de make run no vuelve a escribir nada.
    """
    cursor.execute("""
        SELECT 1 FROM control.archivo_procesado WHERE archivo = %s AND hash_sha256 = %s
        UNION ALL
        SELECT 1 FROM cuarentena.archivo_rechazado WHERE archivo = %s AND hash_sha256 = %s""",
        (archivo, huella, archivo, huella))
    return cursor.fetchone() is not None


def registrar_procesado(cursor, archivo, tipo, huella, filas_leidas, filas_cargadas):
    """Anota en control.archivo_procesado un maestro o una facturación ya cargados (Paso 7).

    Los consumos usan su propio INSERT con todos los conteos de calidad.
    """
    cursor.execute("""
        INSERT INTO control.archivo_procesado (archivo, tipo, hash_sha256, estado, filas_leidas, filas_cargadas)
        VALUES (%s, %s, %s, 'CARGADO', %s, %s)
        ON CONFLICT (archivo) DO UPDATE SET
            tipo = EXCLUDED.tipo, hash_sha256 = EXCLUDED.hash_sha256, estado = EXCLUDED.estado,
            filas_leidas = EXCLUDED.filas_leidas, filas_cargadas = EXCLUDED.filas_cargadas,
            procesado_en = now()""",
        (archivo, tipo, huella, filas_leidas, filas_cargadas))


def registrar_bitacora(conexion, corrida_id, etapa, inicio, fin, resultado,
                       filas_leidas=None, filas_cargadas=None, filas_rechazadas=None, tipo_error=None):
    """Agrega UNA fila a audit.bitacora_corridas, en su propia transacción.

    [README C5] quién (usuario_bd lo pone Postgres: current_user), qué (etapa),
    cuándo (inicio, fin), cuántas filas y con qué resultado. Solo el TIPO de
    error, nunca el mensaje (puede traer valores de una fila → PII).
    """
    with conexion.cursor() as cursor:
        cursor.execute("""
            INSERT INTO audit.bitacora_corridas
                (corrida_id, etapa, inicio, fin, filas_leidas, filas_cargadas, filas_rechazadas,
                 resultado, tipo_error)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (corrida_id, etapa, inicio, fin, filas_leidas, filas_cargadas, filas_rechazadas,
             resultado, tipo_error))
    conexion.commit()


def rechazar_archivo(conexion, archivo, huella, motivo):
    """Registra un archivo rechazado ENTERO, en su propia transacción. Devuelve el resumen.

    Lo usan consumos y facturación: un archivo roto no se carga y no detiene
    la corrida (lo ya cargado nunca se corrompe).
    """
    with conexion.cursor() as cursor:
        cursor.execute("""
            INSERT INTO cuarentena.archivo_rechazado (archivo, hash_sha256, motivo)
            VALUES (%s, %s, %s) ON CONFLICT (archivo, hash_sha256) DO NOTHING""",
            (archivo, huella, motivo))
    conexion.commit()
    return {"archivo": archivo, "estado": "RECHAZADO", "motivo": motivo}
