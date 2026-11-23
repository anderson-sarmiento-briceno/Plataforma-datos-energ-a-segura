"""
Facturación (Paso 6, regla B6).

Cada archivo es un JSON anidado:
    {periodo, emisor, facturas: [ {factura_id, frontera_id, cliente{nit, razon_social,
     email_facturacion}, consumo_facturado_kwh, tarifa_cop_kwh, valor_total_cop,
     componentes: [{concepto, valor_cop_kwh}, ...], metodo_pago{tipo, cuenta_enmascarada}} ]}

Se "aplana" en dos tablas del modelo:
    factura      → core.fact_factura            (una fila por factura)
    componentes  → core.fact_factura_componente (una fila por concepto G, T, D, C, P, R)

[README B6] Una factura cuya frontera no existe en el maestro NO se carga: va a
cuarentena.factura_huerfana (solo IDs, sin PII) y se reporta.

PII de la factura (Paso 7): el NIT del cliente se guarda solo como nit_hash
(HMAC, el mismo de dim_cliente: se cruzan sin ver el NIT) y el email, cifrado
(qué se cifra lo dice data_classification.yaml). La razón social de la factura
no se guarda: ya está en dim_cliente (no se duplica un dato).

Idempotencia: como el email se cifra con nonce aleatorio, un archivo se SALTA
si su huella (SHA-256) no cambió; si no, recargarlo cambiaría los bytes.

Origen de cada decisión: [Exploración] / [README] / [Prevención], como en lectores.py.
"""
import glob
import json
import os
from decimal import Decimal

import psycopg2

from src.bd import hash_archivo, insertar_lote, rechazar_archivo, registrar_procesado, ya_procesado
from src.cifrado import cifrar, huella_nit
from src.clasificacion import columnas_a_cifrar
from src.lectores import ArchivoInvalido

SQL_FACTURA = """
    INSERT INTO core.fact_factura
        (factura_id, frontera_id, periodo, consumo_facturado_kwh, tarifa_cop_kwh, valor_total_cop,
         metodo_pago_tipo, cuenta_enmascarada, archivo_origen, nit_hash, email_facturacion_cifrado)
    VALUES %s
    ON CONFLICT (factura_id) DO UPDATE SET
        frontera_id = EXCLUDED.frontera_id, periodo = EXCLUDED.periodo,
        consumo_facturado_kwh = EXCLUDED.consumo_facturado_kwh,
        tarifa_cop_kwh = EXCLUDED.tarifa_cop_kwh, valor_total_cop = EXCLUDED.valor_total_cop,
        metodo_pago_tipo = EXCLUDED.metodo_pago_tipo,
        cuenta_enmascarada = EXCLUDED.cuenta_enmascarada, archivo_origen = EXCLUDED.archivo_origen,
        nit_hash = EXCLUDED.nit_hash, email_facturacion_cifrado = EXCLUDED.email_facturacion_cifrado"""

SQL_COMPONENTE = """
    INSERT INTO core.fact_factura_componente (factura_id, concepto, valor_cop_kwh)
    VALUES %s
    ON CONFLICT (factura_id, concepto) DO UPDATE SET valor_cop_kwh = EXCLUDED.valor_cop_kwh"""

SQL_HUERFANA = """
    INSERT INTO cuarentena.factura_huerfana (archivo_origen, factura_id, frontera_id, periodo, motivo)
    VALUES %s
    ON CONFLICT (archivo_origen, factura_id) DO NOTHING"""


def listar_archivos_facturacion(carpeta):
    """Todos los .json de la carpeta, en orden de nombre (sin lista fija de meses)."""
    return sorted(glob.glob(os.path.join(carpeta, "*.json")), key=os.path.basename)


def leer_facturacion(ruta):
    """Lee un archivo de facturación. Devuelve (periodo, lista de facturas).

    [README] números exactos: parse_float=Decimal guarda "1234.10" tal cual; con
    float podría quedar 1234.0999… (float no representa exacto muchos decimales).
    [Prevención] JSON roto o sin las llaves esperadas → ArchivoInvalido: el
    archivo se rechaza entero y la corrida sigue.
    """
    try:
        with open(ruta, encoding="utf-8") as f:
            documento = json.load(f, parse_float=Decimal)
        periodo, facturas = documento["periodo"], documento["facturas"]
        if not isinstance(facturas, list):
            raise TypeError
    except (json.JSONDecodeError, UnicodeDecodeError, KeyError, TypeError):
        raise ArchivoInvalido("JSON_INVALIDO")
    return periodo, facturas


def separar_huerfanas(facturas, fronteras_validas):
    """B6: devuelve (validas, huerfanas). Huérfana = su frontera no está en el maestro.

    [Exploración] 3 por mes (12 en total), de 12 fronteras distintas, sin componentes.
    """
    validas = [f for f in facturas if f.get("frontera_id") in fronteras_validas]
    huerfanas = [f for f in facturas if f.get("frontera_id") not in fronteras_validas]
    return validas, huerfanas


def aplanar_factura(factura, periodo, archivo):
    """Convierte una factura anidada en (fila de fact_factura, filas de componentes).

    Solo se toman los campos que no son PII. .get() en lo opcional: si una
    factura no trae método de pago, queda NULL en vez de romper la carga.
    """
    pago = factura.get("metodo_pago") or {}
    fila = (factura["factura_id"], factura["frontera_id"], periodo,
            factura.get("consumo_facturado_kwh"), factura.get("tarifa_cop_kwh"),
            factura.get("valor_total_cop"), pago.get("tipo"), pago.get("cuenta_enmascarada"),
            archivo)
    componentes = [(factura["factura_id"], c["concepto"], c["valor_cop_kwh"])
                   for c in factura.get("componentes") or []]
    return fila, componentes


def pii_factura(factura, seguridad):
    """(nit_hash, email cifrado) del cliente de la factura. Nunca el dato en claro.

    Qué campo se cifra lo dice data_classification.yaml (core.fact_factura).
    """
    cliente = factura.get("cliente") or {}
    mapa = columnas_a_cifrar(seguridad["clasificacion"], "core.fact_factura")
    origen_email = mapa.get("email_facturacion_cifrado")
    email = cifrar(cliente.get(origen_email), seguridad["llave"]) if origen_email else None
    return huella_nit(cliente.get("nit"), seguridad["llave_hmac"]), email


def cargar_archivo_facturacion(conexion, ruta, fronteras_validas, tamano_lote, seguridad):
    """Carga UN archivo de facturación en UNA transacción. Devuelve un resumen (solo conteos)."""
    archivo = os.path.basename(ruta)
    huella = hash_archivo(ruta)
    with conexion.cursor() as cursor:
        saltar = ya_procesado(cursor, archivo, huella)
    conexion.rollback()   # cierra la transacción que abrió el SELECT
    if saltar:
        return {"archivo": archivo, "estado": "SALTADO"}
    try:
        periodo, facturas = leer_facturacion(ruta)
    except ArchivoInvalido as error:
        return rechazar_archivo(conexion, archivo, huella, error.motivo)

    validas, huerfanas = separar_huerfanas(facturas, fronteras_validas)
    # [Prevención] Si un factura_id viniera repetido en el mismo archivo, el
    # upsert fallaría ("no puede afectar la misma fila dos veces"): queda la última.
    por_id = {f["factura_id"]: f for f in validas}
    filas, componentes = [], []
    for factura in por_id.values():
        fila, comps = aplanar_factura(factura, periodo, archivo)
        filas.append(fila + pii_factura(factura, seguridad))
        componentes.extend(comps)
    filas_huerfanas = [(archivo, f.get("factura_id"), f.get("frontera_id"), periodo, "FRONTERA_NO_EXISTE")
                       for f in huerfanas]
    try:
        with conexion.cursor() as cursor:
            insertar_lote(cursor, SQL_FACTURA, filas, tamano_lote)           # primero la factura (FK)
            insertar_lote(cursor, SQL_COMPONENTE, componentes, tamano_lote)
            insertar_lote(cursor, SQL_HUERFANA, filas_huerfanas, tamano_lote)
            registrar_procesado(cursor, archivo, "facturacion", huella, len(facturas), len(filas))
        conexion.commit()
    except (psycopg2.DataError, psycopg2.IntegrityError) as error:
        # Mismo criterio que en consumos: un dato que la base rechaza no tumba la corrida.
        conexion.rollback()
        return rechazar_archivo(conexion, archivo, huella, f"ERROR_CARGA:{type(error).__name__}")
    return {"archivo": archivo, "estado": "CARGADO", "facturas_leidas": len(facturas),
            "facturas_cargadas": len(filas), "componentes": len(componentes),
            "huerfanas": len(huerfanas)}


def cargar_facturacion(conexion, carpeta, fronteras_validas, tamano_lote, seguridad):
    """Carga todos los archivos de facturación de la carpeta. Devuelve un resumen por archivo."""
    return [cargar_archivo_facturacion(conexion, ruta, fronteras_validas, tamano_lote, seguridad)
            for ruta in listar_archivos_facturacion(carpeta)]
