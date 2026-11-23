"""
Carga de maestros: clientes, fronteras y límites.

Se cargan ANTES que los consumos porque las tablas tienen FK:
  fact_consumo_horario → dim_frontera → dim_cliente.

Paso 7 (seguridad) en los clientes:
  - [C1] data_classification.yaml dice qué columnas son Restringido-PII y de
    qué columna del CSV salen; esas se guardan CIFRADAS (AES-256-GCM).
  - [C2] observaciones: se guarda enmascarada (y la original, cifrada).
  - [C3] nit_hash = HMAC del NIT normalizado, para cruzar con facturas sin verlo.
  Nunca entra PII en claro a la base.

Idempotencia: el cifrado usa un nonce aleatorio (el mismo dato da bytes
distintos cada vez). Si los maestros se recargaran en cada corrida, la tabla
cambiaría aunque los archivos no. Por eso se SALTAN si la huella (SHA-256) de
los tres archivos no cambió, igual que los consumos.
"""
import csv
import io
import os
from decimal import Decimal

from src.bd import hash_archivo, insertar_lote, registrar_procesado, ya_procesado
from src.cifrado import cifrar, huella_nit
from src.clasificacion import columnas_a_cifrar
from src.lectores import detectar_encoding
from src.pii import enmascarar, tiene_pii

ARCHIVOS = ("clientes.csv", "fronteras.csv", "limites_frontera.csv")

# Columnas BYTEA de core.dim_cliente, en el orden del INSERT. Qué se cifra en
# cada una (y desde qué columna del CSV) lo dice data_classification.yaml.
COLUMNAS_CIFRADAS_CLIENTE = ["nit_cifrado", "email_cifrado", "celular_cifrado",
                             "direccion_cifrada", "representante_legal_cifrado",
                             "observaciones_cifrada"]

SQL_CLIENTE = f"""
    INSERT INTO core.dim_cliente
        (cliente_id, razon_social, nit_hash, {", ".join(COLUMNAS_CIFRADAS_CLIENTE)},
         ciudad, departamento, observaciones_enmascarada, archivo_origen)
    VALUES %s
    ON CONFLICT (cliente_id) DO UPDATE SET
        razon_social = EXCLUDED.razon_social, nit_hash = EXCLUDED.nit_hash,
        {", ".join(f"{c} = EXCLUDED.{c}" for c in COLUMNAS_CIFRADAS_CLIENTE)},
        ciudad = EXCLUDED.ciudad, departamento = EXCLUDED.departamento,
        observaciones_enmascarada = EXCLUDED.observaciones_enmascarada,
        archivo_origen = EXCLUDED.archivo_origen"""
# (Los nombres de columna del f-string son constantes del código, no datos:
#  los VALORES siempre viajan por %s.)


def leer_csv_maestro(ruta):
    """Lee un maestro con el módulo csv y devuelve una lista de diccionarios.

    [Exploración] clientes.csv viene en Latin-1 y trae saltos de línea y comillas
    DENTRO de campos (CLI0013, CLI0022): el módulo csv respeta las comillas;
    un split(",") partiría esas filas. [README B7]
    """
    with open(ruta, "rb") as f:
        contenido = f.read()
    texto = contenido.decode(detectar_encoding(contenido))
    return list(csv.DictReader(io.StringIO(texto)))


def contar_clientes_con_pii(carpeta):
    """[README C2] Cuántos clientes tienen PII en observaciones (para el dq_report).

    Se calcula leyendo el archivo en cada corrida (es barato), así el número
    está aunque la carga de maestros se salte.
    """
    filas = leer_csv_maestro(os.path.join(carpeta, "clientes.csv"))
    return sum(1 for f in filas if tiene_pii(f.get("observaciones")))


def fila_cliente(fila, seguridad, archivo):
    """Arma la tupla de un cliente: PII cifrada, NIT como huella, observaciones enmascaradas."""
    mapa = columnas_a_cifrar(seguridad["clasificacion"], "core.dim_cliente")
    cifradas = [cifrar(fila.get(mapa[c]), seguridad["llave"]) if c in mapa else None
                for c in COLUMNAS_CIFRADAS_CLIENTE]
    observaciones, _ = enmascarar(fila.get("observaciones"))
    return (fila["cliente_id"].strip(), fila.get("razon_social"),
            huella_nit(fila.get("nit"), seguridad["llave_hmac"]), *cifradas,
            fila.get("ciudad"), fila.get("departamento"), observaciones, archivo)


def cargar_clientes(cursor, carpeta, tamano_lote, seguridad):
    """Upsert de clientes con la PII protegida. Devuelve (cargados, rechazados).

    [README B7] Ningún cliente se pierde en silencio: el que no tiene
    cliente_id va a cuarentena.cliente_rechazado (solo fila y motivo, sin PII).
    razon_social se guarda TAL CUAL (CLI0036 incluido): con %s es un dato, no código.
    """
    mapa = columnas_a_cifrar(seguridad["clasificacion"], "core.dim_cliente")
    sobrantes = set(mapa) - set(COLUMNAS_CIFRADAS_CLIENTE)
    if sobrantes:
        # El YAML pide cifrar en una columna que la tabla no tiene: mejor fallar que guardar en claro.
        raise RuntimeError(f"data_classification.yaml pide columnas cifradas que no existen: {sobrantes}")

    ruta = os.path.join(carpeta, "clientes.csv")
    archivo = os.path.basename(ruta)
    buenos, rechazados = [], []
    for numero, fila in enumerate(leer_csv_maestro(ruta), start=1):
        if not (fila.get("cliente_id") or "").strip():
            rechazados.append((archivo, numero, None, "CLIENTE_SIN_ID"))
            continue
        buenos.append(fila_cliente(fila, seguridad, archivo))

    insertar_lote(cursor, SQL_CLIENTE, buenos, tamano_lote)
    insertar_lote(cursor, """
        INSERT INTO cuarentena.cliente_rechazado (archivo_origen, fila_origen, cliente_id, motivo)
        VALUES %s ON CONFLICT (archivo_origen, fila_origen) DO NOTHING""",
        rechazados, tamano_lote)
    return len(buenos), len(rechazados)


def cargar_fronteras(cursor, carpeta, tamano_lote):
    """Upsert de fronteras. Devuelve cuántas se cargaron."""
    ruta = os.path.join(carpeta, "fronteras.csv")
    filas = [(f["frontera_id"].strip(), f["codigo_sic"], f["cliente_id"].strip(), f["mercado"],
              f["operador_red"], int(f["nivel_tension"]), f["fecha_registro"], f["estado"],
              os.path.basename(ruta))
             for f in leer_csv_maestro(ruta)]
    insertar_lote(cursor, """
        INSERT INTO core.dim_frontera (frontera_id, codigo_sic, cliente_id, mercado, operador_red,
                                       nivel_tension, fecha_registro, estado, archivo_origen)
        VALUES %s
        ON CONFLICT (frontera_id) DO UPDATE SET
            codigo_sic = EXCLUDED.codigo_sic, cliente_id = EXCLUDED.cliente_id,
            mercado = EXCLUDED.mercado, operador_red = EXCLUDED.operador_red,
            nivel_tension = EXCLUDED.nivel_tension, fecha_registro = EXCLUDED.fecha_registro,
            estado = EXCLUDED.estado, archivo_origen = EXCLUDED.archivo_origen""",
        filas, tamano_lote)
    return len(filas)


def cargar_limites(cursor, carpeta, tamano_lote):
    """Upsert de límites por frontera (los usa el streaming). Devuelve cuántos."""
    ruta = os.path.join(carpeta, "limites_frontera.csv")
    filas = [(f["frontera_id"].strip(), Decimal(f["min_kwh"]), Decimal(f["max_kwh"]),
              os.path.basename(ruta))
             for f in leer_csv_maestro(ruta)]
    insertar_lote(cursor, """
        INSERT INTO core.dim_limite_frontera (frontera_id, min_kwh, max_kwh, archivo_origen)
        VALUES %s
        ON CONFLICT (frontera_id) DO UPDATE SET
            min_kwh = EXCLUDED.min_kwh, max_kwh = EXCLUDED.max_kwh,
            archivo_origen = EXCLUDED.archivo_origen""",
        filas, tamano_lote)
    return len(filas)


def cargar_maestros(conexion, carpeta, tamano_lote, seguridad):
    """Carga clientes, fronteras y límites en UNA transacción (en ese orden, por las FK).

    Si ninguno de los tres archivos cambió desde la última carga, se saltan.
    """
    huellas = {nombre: hash_archivo(os.path.join(carpeta, nombre)) for nombre in ARCHIVOS}
    resumen = {"clientes_con_pii": contar_clientes_con_pii(carpeta)}
    try:
        with conexion.cursor() as cursor:
            if all(ya_procesado(cursor, nombre, huella) for nombre, huella in huellas.items()):
                conexion.rollback()
                return {"estado": "SALTADO", **resumen}
            clientes, rechazados = cargar_clientes(cursor, carpeta, tamano_lote, seguridad)
            fronteras = cargar_fronteras(cursor, carpeta, tamano_lote)
            limites = cargar_limites(cursor, carpeta, tamano_lote)
            for nombre, cargadas in (("clientes.csv", clientes), ("fronteras.csv", fronteras),
                                     ("limites_frontera.csv", limites)):
                registrar_procesado(cursor, nombre, "maestro", huellas[nombre],
                                    cargadas + (rechazados if nombre == "clientes.csv" else 0), cargadas)
        conexion.commit()
    except Exception:
        conexion.rollback()
        raise
    return {"estado": "CARGADO", "clientes": clientes, "clientes_rechazados": rechazados,
            "fronteras": fronteras, "limites": limites, **resumen}
