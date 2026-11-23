"""
Pipeline batch: python -m src.pipeline [--datos CARPETA]   (lo llama `make run`)

Es el "director de orquesta": no tiene reglas propias, solo llama a cada pieza
en orden y maneja las transacciones.

    1. maestros  (clientes sin PII, fronteras, límites)      → src/maestros.py
    (al final) facturación: aplanar JSON, huérfanas a cuarentena (B6) → src/facturacion.py
    2. por cada archivo de consumos, en orden lexicográfico:
         ¿ya procesado con el mismo hash? → se salta          (idempotencia)
         leer y traducir                                       → src/lectores.py
         ¿estructura rota? → cuarentena.archivo_rechazado
         B4 inválidos → cuarentena                            → src/calidad.py
         B3 duplicados dentro del archivo → cuarentena
         B3 contra lo ya cargado (correcciones tardías)
         upsert por lotes + control.archivo_procesado
       todo lo de un archivo en UNA transacción: si algo falla, ROLLBACK y lo
       ya cargado no cambia: los meses ya cargados quedan intactos.

Al final: out/consumo_mensual.csv, out/dq_report.json, out/metrics.json
(src/salidas.py) y logs JSON en logs/pipeline.log (src/logs.py).
"""
import argparse
import logging
import os
import sys
import uuid
from datetime import datetime, timezone

import pandas as pd
import psycopg2

from src import calidad
from src.bd import (conectar, hash_archivo, insertar_lote, rechazar_archivo, registrar_bitacora,
                    ya_procesado)
from src.cifrado import cargar_llave
from src.clasificacion import cargar_clasificacion
from src.config import RAIZ, cargar_config, ruta_datos
from src.facturacion import cargar_facturacion
from src.lectores import ArchivoInvalido, leer_archivo_consumo, listar_archivos_consumo
from src.logs import configurar_logs
from src.maestros import cargar_maestros
from src.salidas import armar_metrics, escribir_json, generar_salidas

log = logging.getLogger("pipeline")

# [README B3] Segunda red de seguridad: aunque pandas ya decidió quién gana,
# la base solo reemplaza si la nueva lectura gana por (versión, archivo, fila).
# COLLATE "C" = orden por bytes, igual que Python. Con una intercalación de
# idioma (p. ej. ICU) "…04_correccion.csv" quedaría ANTES que "…04.csv"
# porque ignora la puntuación (verificado en Postgres).
SQL_UPSERT_CONSUMO = """
    INSERT INTO core.fact_consumo_horario AS t
        (frontera_id, ts_medicion, kwh, version_lectura, calidad_lectura, archivo_origen, fila_origen)
    VALUES %s
    ON CONFLICT (frontera_id, ts_medicion) DO UPDATE SET
        kwh = EXCLUDED.kwh, version_lectura = EXCLUDED.version_lectura,
        calidad_lectura = EXCLUDED.calidad_lectura, archivo_origen = EXCLUDED.archivo_origen,
        fila_origen = EXCLUDED.fila_origen, cargado_en = now()
    WHERE (EXCLUDED.version_lectura, EXCLUDED.archivo_origen COLLATE "C", EXCLUDED.fila_origen)
       >= (t.version_lectura, t.archivo_origen COLLATE "C", t.fila_origen)"""

# Re-correr no duplica rechazos: PK (archivo, fila) + DO NOTHING.
SQL_CUARENTENA_CONSUMO = """
    INSERT INTO cuarentena.consumo_rechazado
        (archivo_origen, fila_origen, motivo, frontera_id, ts_texto, valor_texto, unidad, version_texto)
    VALUES %s
    ON CONFLICT (archivo_origen, fila_origen) DO NOTHING"""

SQL_CONTROL = """
    INSERT INTO control.archivo_procesado
        (archivo, hash_sha256, estado, filas_leidas, filas_cargadas, nulos, negativos, outliers,
         mwh_convertidos, duplicados_exactos, duplicados_conflictivos, correcciones_aplicadas)
    VALUES (%(archivo)s, %(hash)s, 'CARGADO', %(filas_leidas)s, %(filas_cargadas)s, %(nulos)s,
            %(negativos)s, %(outliers)s, %(mwh_convertidos)s, %(duplicados_exactos)s,
            %(duplicados_conflictivos)s, %(correcciones_aplicadas)s)
    ON CONFLICT (archivo) DO UPDATE SET
        hash_sha256 = EXCLUDED.hash_sha256, estado = EXCLUDED.estado,
        filas_leidas = EXCLUDED.filas_leidas, filas_cargadas = EXCLUDED.filas_cargadas,
        nulos = EXCLUDED.nulos, negativos = EXCLUDED.negativos, outliers = EXCLUDED.outliers,
        mwh_convertidos = EXCLUDED.mwh_convertidos,
        duplicados_exactos = EXCLUDED.duplicados_exactos,
        duplicados_conflictivos = EXCLUDED.duplicados_conflictivos,
        correcciones_aplicadas = EXCLUDED.correcciones_aplicadas, procesado_en = now()"""


def leer_existentes(cursor, ganadoras):
    """Lecturas ya cargadas en el rango de horas del archivo (para B3 entre archivos)."""
    columnas = ["frontera_id", "ts_medicion", "version_lectura", "archivo_origen", "fila_origen"]
    if ganadoras.empty:
        return pd.DataFrame(columns=columnas)
    cursor.execute(f"""
        SELECT {", ".join(columnas)} FROM core.fact_consumo_horario
        WHERE ts_medicion BETWEEN %s AND %s""",
        (ganadoras["ts_medicion"].min().to_pydatetime(), ganadoras["ts_medicion"].max().to_pydatetime()))
    return pd.DataFrame(cursor.fetchall(), columns=columnas)


def filas_para_hecho(df):
    """Tuplas con tipos de Python (psycopg2 no entiende los enteros de numpy)."""
    return [(f.frontera_id, f.ts_medicion.to_pydatetime(), f.kwh, int(f.version_lectura),
             f.calidad_lectura, f.archivo_origen, int(f.fila_origen))
            for f in df.itertuples()]


def filas_para_cuarentena(df):
    """La fila tal como vino (texto) + motivo. Los consumos no tienen PII."""
    return [(f.archivo_origen, int(f.fila_origen), f.motivo, f.frontera_id, f.ts_texto,
             f.valor_texto, f.unidad, f.version_texto)
            for f in df.itertuples()]


def procesar_archivo(conexion, ruta, fronteras_validas, tamano_lote):
    """Procesa UN archivo de consumos en UNA transacción. Devuelve un resumen (solo conteos)."""
    archivo = os.path.basename(ruta)
    huella = hash_archivo(ruta)
    try:
        with conexion.cursor() as cursor:
            if ya_procesado(cursor, archivo, huella):
                # Revisión /code-review: el SELECT abre una transacción; se cierra
                # para no dejarla abierta ("idle in transaction") al saltar.
                conexion.rollback()
                return {"archivo": archivo, "estado": "SALTADO"}

            try:
                df = leer_archivo_consumo(ruta)
            except ArchivoInvalido as error:
                # Archivo truncado/corrupto: se rechaza entero, no se carga nada de él.
                conexion.rollback()
                return rechazar_archivo(conexion, archivo, huella, error.motivo)

            # Revisión /code-review: si este archivo ya se había cargado con OTRO
            # contenido (llegó de nuevo, corregido), se borra primero lo que venía
            # de él (hecho y cuarentena) y se carga la versión nueva. Así no se
            # compara consigo mismo ni quedan rechazos viejos. Dentro de la misma
            # transacción: si algo falla, el ROLLBACK devuelve lo anterior.
            # La primera vez no borra nada (no hay filas de ese archivo).
            cursor.execute("DELETE FROM core.fact_consumo_horario WHERE archivo_origen = %s", (archivo,))
            cursor.execute("DELETE FROM cuarentena.consumo_rechazado WHERE archivo_origen = %s", (archivo,))

            validas, rechazadas = calidad.separar_invalidos(df, fronteras_validas)      # B4
            ganadoras, sobrantes = calidad.resolver_duplicados(validas)                 # B3 en el archivo
            existentes = leer_existentes(cursor, ganadoras)
            a_cargar, superadas, correcciones = calidad.resolver_contra_existentes(    # B3 entre archivos
                ganadoras, existentes)

            insertar_lote(cursor, SQL_UPSERT_CONSUMO, filas_para_hecho(a_cargar), tamano_lote)
            insertar_lote(cursor, SQL_CUARENTENA_CONSUMO,
                          filas_para_cuarentena(pd.concat([rechazadas, sobrantes, superadas])),
                          tamano_lote)
            conteos = {"archivo": archivo, "hash": huella, "filas_leidas": len(df),
                       "filas_cargadas": len(a_cargar), "correcciones_aplicadas": correcciones,
                       **calidad.contar(rechazadas, sobrantes, validas)}
            cursor.execute(SQL_CONTROL, conteos)
        conexion.commit()
    except (psycopg2.DataError, psycopg2.IntegrityError) as error:
        # Revisión /code-review: un dato que la base rechaza (CHECK, FK, número
        # fuera de rango) en UN archivo no puede tumbar todo make run.
        # Lo importante no es no fallar, sino no corromper:
        # ROLLBACK de este archivo, se registra como rechazado y se sigue.
        # Solo el TIPO de error: el mensaje de Postgres puede traer valores.
        conexion.rollback()
        return rechazar_archivo(conexion, archivo, huella, f"ERROR_CARGA:{type(error).__name__}")
    except Exception:
        # Cualquier otro error (p. ej. se cayó la base) sí detiene la corrida:
        # seguir sin base no tiene sentido. Nada de este archivo queda a medias.
        conexion.rollback()
        raise
    conteos.pop("hash")
    return {"estado": "CARGADO", **conteos}


def ejecutar_etapa(conexion, corrida_id, etapa, funcion, etapas):
    """Corre una etapa, mide su duración y la anota en la bitácora (C5), salga bien o mal.

    `funcion` devuelve (filas_leidas, filas_cargadas, filas_rechazadas, detalle).
    Si falla: ROLLBACK, se anota la etapa con resultado ERROR y SOLO el tipo de
    error, y el error sigue hacia arriba (la corrida termina con exit 1).
    `etapas` junta las duraciones para metrics.json (Paso 8).
    """
    inicio = datetime.now(timezone.utc)
    try:
        leidas, cargadas, rechazadas, detalle = funcion()
    except Exception as error:
        conexion.rollback()
        try:
            registrar_bitacora(conexion, corrida_id, etapa, inicio, datetime.now(timezone.utc),
                               "ERROR", tipo_error=type(error).__name__)
        except Exception:
            log.error("bitacora_no_escrita", extra={"datos": {"etapa": etapa}})
        raise
    fin = datetime.now(timezone.utc)
    registrar_bitacora(conexion, corrida_id, etapa, inicio, fin, "OK", leidas, cargadas, rechazadas)
    etapas.append({"etapa": etapa, "duracion_s": round((fin - inicio).total_seconds(), 3),
                   "filas_leidas": leidas, "filas_cargadas": cargadas, "filas_rechazadas": rechazadas})
    return detalle


def etapa_maestros(conexion, carpeta, tamano_lote, seguridad):
    """Maestros → (leídas, cargadas, rechazadas, resumen)."""
    resumen = cargar_maestros(conexion, os.path.join(carpeta, "maestros"), tamano_lote, seguridad)
    log.info("maestros", extra={"datos": resumen})
    if resumen["estado"] == "SALTADO":
        return 0, 0, 0, resumen
    cargadas = resumen["clientes"] + resumen["fronteras"] + resumen["limites"]
    return cargadas + resumen["clientes_rechazados"], cargadas, resumen["clientes_rechazados"], resumen


def etapa_consumos(conexion, carpeta, tamano_lote):
    """Todos los archivos de consumos → (leídas, cargadas, rechazadas, lista de resúmenes)."""
    with conexion.cursor() as cursor:
        cursor.execute("SELECT frontera_id FROM core.dim_frontera")
        fronteras_validas = {fila[0] for fila in cursor.fetchall()}
    conexion.rollback()   # cierra la transacción del SELECT
    resumenes = []
    for ruta in listar_archivos_consumo(os.path.join(carpeta, "consumos")):
        resumen = procesar_archivo(conexion, ruta, fronteras_validas, tamano_lote)
        log.info("consumos", extra={"datos": resumen})
        resumenes.append(resumen)
    leidas = sum(r.get("filas_leidas", 0) for r in resumenes)
    cargadas = sum(r.get("filas_cargadas", 0) for r in resumenes)
    return leidas, cargadas, leidas - cargadas, resumenes


def etapa_facturacion(conexion, carpeta, tamano_lote, seguridad):
    """B6: facturación → (leídas, cargadas, huérfanas, lista de resúmenes)."""
    with conexion.cursor() as cursor:
        cursor.execute("SELECT frontera_id FROM core.dim_frontera")
        fronteras_validas = {fila[0] for fila in cursor.fetchall()}
    conexion.rollback()
    resumenes = cargar_facturacion(conexion, os.path.join(carpeta, "facturacion"),
                                   fronteras_validas, tamano_lote, seguridad)
    for resumen in resumenes:
        log.info("facturacion", extra={"datos": resumen})
    return (sum(r.get("facturas_leidas", 0) for r in resumenes),
            sum(r.get("facturas_cargadas", 0) for r in resumenes),
            sum(r.get("huerfanas", 0) for r in resumenes), resumenes)


def etapa_salidas(conexion, carpeta_out, clientes_con_pii):
    """Paso 8: consumo_mensual.csv (con el rol analista, ADR-005) y dq_report.json."""
    conexion_analista = conectar("analista")
    try:
        filas, reporte = generar_salidas(conexion, conexion_analista, carpeta_out, clientes_con_pii)
    finally:
        conexion_analista.close()
    # En el log van los totales; el detalle por frontera queda solo en el archivo.
    log.info("salidas", extra={"datos": {
        "archivos": ["consumo_mensual.csv", "dq_report.json"], "filas_consumo_mensual": len(filas),
        **{k: v for k, v in reporte.items() if k != "horas_sin_lectura_valida_por_frontera"}}})
    return len(filas), len(filas), 0, reporte


def main(argumentos=None):
    """Corre el pipeline completo. Devuelve el código de salida (0 = bien)."""
    parser = argparse.ArgumentParser(description="Pipeline batch de consumos")
    parser.add_argument("--datos", help="carpeta con consumos/ y maestros/ (por defecto: data/)")
    args = parser.parse_args(argumentos)

    config = cargar_config()
    carpeta = args.datos or ruta_datos(config)
    tamano_lote = config["pipeline"]["batch_size"]
    carpeta_out = os.path.join(RAIZ, config["salidas"]["carpeta_out"])
    # Logs JSON con filtro anti-PII (src/logs.py). Solo conteos y nombres de archivo.
    configurar_logs(os.path.join(RAIZ, config["salidas"]["carpeta_logs"]))
    # Paso 7: llaves (de .env, nunca de la base) y clasificación (qué se cifra).
    # Si falta una llave, falla aquí con un mensaje claro, antes de tocar la base.
    seguridad = {"llave": cargar_llave("LLAVE_CIFRADO"), "llave_hmac": cargar_llave("LLAVE_HMAC"),
                 "clasificacion": cargar_clasificacion()}

    # [README C4] Se conecta como rol de ingesta, no como superusuario.
    conexion = conectar("ingesta")
    corrida_id = str(uuid.uuid4())   # identifica todas las filas de bitácora de esta corrida
    inicio = datetime.now(timezone.utc)
    etapas = []
    log.info("inicio_corrida", extra={"datos": {"corrida_id": corrida_id}})
    try:
        maestros = ejecutar_etapa(conexion, corrida_id, "maestros", lambda: etapa_maestros(
            conexion, carpeta, tamano_lote, seguridad), etapas)
        ejecutar_etapa(conexion, corrida_id, "consumos", lambda: etapa_consumos(
            conexion, carpeta, tamano_lote), etapas)
        # B6: después de los maestros (FK a dim_frontera). Independiente de los consumos.
        ejecutar_etapa(conexion, corrida_id, "facturacion", lambda: etapa_facturacion(
            conexion, carpeta, tamano_lote, seguridad), etapas)
        ejecutar_etapa(conexion, corrida_id, "salidas", lambda: etapa_salidas(
            conexion, carpeta_out, maestros["clientes_con_pii"]), etapas)
        fin = datetime.now(timezone.utc)
        registrar_bitacora(conexion, corrida_id, "corrida", inicio, fin, "OK",
                           sum(e["filas_leidas"] for e in etapas), sum(e["filas_cargadas"] for e in etapas),
                           sum(e["filas_rechazadas"] for e in etapas))
        # metrics.json va al final: así incluye la duración de todas las etapas.
        with conexion.cursor() as cursor:
            escribir_json(armar_metrics(cursor, corrida_id, inicio, fin, etapas),
                          os.path.join(carpeta_out, "metrics.json"))
        conexion.rollback()
        log.info("fin_corrida", extra={"datos": {"corrida_id": corrida_id, "resultado": "OK",
                                                 "duracion_s": round((fin - inicio).total_seconds(), 3)}})
    except Exception as error:
        # Solo el TIPO de error: el mensaje de Postgres puede traer valores de una fila.
        log.error("fin_corrida", extra={"datos": {"corrida_id": corrida_id, "resultado": "ERROR",
                                                  "tipo_error": type(error).__name__}})
        try:
            registrar_bitacora(conexion, corrida_id, "corrida", inicio, datetime.now(timezone.utc),
                               "ERROR", tipo_error=type(error).__name__)
        except Exception:
            log.error("bitacora_no_escrita", extra={"datos": {"etapa": "corrida"}})
        return 1
    finally:
        conexion.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
