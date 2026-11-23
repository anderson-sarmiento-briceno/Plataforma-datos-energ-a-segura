"""
Lectores de archivos de consumo (Paso 4).

Regla del módulo: el lector TRADUCE, no juzga.
  - Traduce cualquier formato de data/consumos/ a UN solo formato normalizado.
  - No descarta filas: si un valor no se entiende, sale como None y el módulo
    de calidad (Paso 5) decide el motivo de cuarentena. Nada se pierde en silencio.
  - Solo rechaza el ARCHIVO entero si su estructura está rota (ArchivoInvalido).

Cada decisión indica de dónde sale:
  [Exploración]  lo encontró scripts/exploracion.py en los datos reales.
  [README]       lo exige una regla del README (B1–B7, C1–C8, D, E).
  [Prevención]   no está en los datos ni en el README; evita una falla concreta.
"""
import csv
import glob
import io
import os
from decimal import Decimal, InvalidOperation

import pandas as pd

# [README B1] El mes y la hora se definen en hora de Colombia.
ZONA_LOCAL = "America/Bogota"

# [Exploración] Formatos de hora vistos en los 5 archivos. Un mes nuevo debe
# llegar en uno de estos formatos: se usan formatos EXACTOS; una hora escrita de otra forma sale como None → cuarentena (Paso 5).
FORMATO_HORA_LOCAL = "%Y-%m-%d %H:%M:%S"     # enero, marzo, abril: 2026-01-01 00:00:00
FORMATO_HORA_UTC = "%Y-%m-%dT%H:%M:%SZ"      # febrero:             2026-02-01T05:00:00Z

# [Exploración] Nombres de columna que cambian según el mes.
# Para la hora, el nombre dice además si viene en UTC.
ALIAS_HORA = {"timestamp": False, "timestamp_utc": True}   # nombre → ¿viene en UTC?
ALIAS_VALOR = ("consumo_kwh", "kwh")                       # marzo usa "kwh"

# [Exploración + README B2] Marzo trae 400 filas en MWh; el resultado va en kWh.
# Solo las unidades vistas: otra unidad no se adivina, va a cuarentena (Paso 5).
FACTOR_A_KWH = {"kwh": Decimal("1"), "mwh": Decimal("1000")}


class ArchivoInvalido(Exception):
    """El archivo no se puede procesar como un todo.

    [README A] Un archivo truncado o corrupto se rechaza entero. Lleva el motivo para guardarlo en cuarentena.archivo_rechazado.
    """

    def __init__(self, motivo):
        super().__init__(motivo)
        self.motivo = motivo


def listar_archivos_consumo(carpeta):
    """Devuelve las rutas de todos los .csv de la carpeta, en orden lexicográfico.

    [README A] Se procesa lo que haya en data/consumos/, no una lista fija:
    por eso se lista la carpeta en vez de nombrar los meses.
    [README B3] El desempate de duplicados usa el orden lexicográfico de nombre;
    así "consumos_2026-04.csv" queda antes que "consumos_2026-04_correccion.csv".
    """
    return sorted(glob.glob(os.path.join(carpeta, "*.csv")), key=os.path.basename)


def detectar_encoding(contenido):
    """Devuelve el encoding con el que se pueden leer los bytes del archivo.

    [Exploración] Hay archivos en Latin-1 (clientes.csv), no todo es UTF-8.
    Se prueba primero UTF-8; Latin-1 va de segundo porque NUNCA falla (cualquier
    byte es una letra válida) y, si fuera primero, dañaría las tildes de un UTF-8.
    [Prevención] "utf-8-sig" acepta el BOM que agrega Excel al guardar; sin eso
    la primera columna se llamaría "\\ufefffrontera_id" y el archivo se
    rechazaría por "columna faltante".
    """
    try:
        contenido.decode("utf-8-sig")
        return "utf-8-sig"
    except UnicodeDecodeError:
        return "latin-1"


def detectar_separador(encabezado):
    """Devuelve ';' o ',' según cuál aparezca más en el encabezado.

    [Exploración] Febrero usa ';'. Se mira el ENCABEZADO y no los datos, porque
    en febrero los datos traen coma decimal (163,795) y confundirían el conteo.
    """
    return ";" if encabezado.count(";") > encabezado.count(",") else ","


def identificar_columnas(columnas):
    """Encuentra qué columna es la hora, cuál el valor y si hay columna de unidad.

    Devuelve (col_hora, hora_en_utc, col_valor, col_unidad o None).
    [Exploración] La hora se llama "timestamp" o "timestamp_utc"; el valor,
    "consumo_kwh" o "kwh"; solo marzo trae "unidad".
    Si falta una columna obligatoria, el archivo no se puede interpretar →
    ArchivoInvalido (no se adivina qué columna es cuál).
    """
    # El primer alias que exista en el archivo; None si no hay ninguno.
    col_hora = next((c for c in ALIAS_HORA if c in columnas), None)
    col_valor = next((c for c in ALIAS_VALOR if c in columnas), None)
    if (col_hora is None or col_valor is None
            or "frontera_id" not in columnas or "version_lectura" not in columnas):
        raise ArchivoInvalido("COLUMNAS_FALTANTES")
    col_unidad = "unidad" if "unidad" in columnas else None
    return col_hora, ALIAS_HORA[col_hora], col_valor, col_unidad


def convertir_hora(textos, hora_en_utc):
    """Convierte la columna de hora (texto) a instantes en hora de Bogotá.

    [README B1 + Exploración] Febrero viene en UTC ("...Z"): se convierte a
    Bogotá (05:00Z → 00:00 del mismo día). Los demás vienen en hora local: se
    les asigna la zona de Bogotá. Bogotá es UTC−5 fijo, sin horario de verano,
    así que no hay horas ambiguas.
    Una hora vacía o en otro formato queda como NaT (vacío) → cuarentena en el Paso 5.
    """
    if hora_en_utc:
        instantes = pd.to_datetime(textos, format=FORMATO_HORA_UTC, utc=True, errors="coerce")
        return instantes.dt.tz_convert(ZONA_LOCAL)
    instantes = pd.to_datetime(textos, format=FORMATO_HORA_LOCAL, errors="coerce")
    return instantes.dt.tz_localize(ZONA_LOCAL)


def convertir_valor(texto, unidad="kWh"):
    """Convierte un valor de texto a Decimal en kWh, o None si no se puede.

    [Exploración] Febrero usa coma decimal ("163,795" → 163.795).
    [Exploración + README B2] MWh se multiplica por 1000.
    Decimal y no float: las sumas deben dar exactas con 3 decimales.
    [Prevención] Decimal("NaN") e "Infinity" son válidos en Python pero no son
    consumos: se devuelven como None.
    No se juzga si el valor es negativo u outlier: eso es del Paso 5 (B4).
    """
    limpio = texto.strip().replace(",", ".")
    if not limpio:
        return None
    try:
        numero = Decimal(limpio)
    except InvalidOperation:
        return None
    if not numero.is_finite():
        return None
    factor = FACTOR_A_KWH.get(unidad.strip().lower())
    if factor is None:
        return None   # unidad desconocida: no se adivina
    return numero * factor


def convertir_entero(texto):
    """Convierte un texto a int, o None si no es un entero (p. ej. version_lectura)."""
    limpio = texto.strip()
    return int(limpio) if limpio.isdigit() else None


def leer_archivo_consumo(ruta):
    """Lee un archivo de consumos y lo devuelve normalizado.

    Columnas de salida (iguales para todos los meses):
      frontera_id, ts_medicion (instante en Bogotá o NaT), kwh (Decimal o None),
      version_lectura (int o None), calidad_lectura, unidad,
      ts_texto, valor_texto, version_texto (lo que venía, para la cuarentena),
      archivo_origen, fila_origen (1 = primera fila después del encabezado).

    Lanza ArchivoInvalido si la ESTRUCTURA está rota. Si solo hay VALORES malos,
    el archivo pasa y esas filas van a cuarentena en el Paso 5 (contexto §8.5).
    """
    with open(ruta, "rb") as f:
        contenido = f.read()
    if not contenido.strip():
        raise ArchivoInvalido("ARCHIVO_VACIO")

    texto = contenido.decode(detectar_encoding(contenido))
    separador = detectar_separador(texto.splitlines()[0])

    # Se parte con el módulo csv de Python (respeta comillas) y NO con
    # pd.read_csv, porque read_csv corrompe en silencio las filas rotas
    # (verificado en las pruebas del Paso 4):
    #   - fila cortada (menos campos): rellena lo que falta con "" → parece un vacío;
    #   - fila con campos de más: corre las columnas (frontera_id recibe la hora).
    # Contando los campos de cada fila se detectan las dos cosas.
    try:
        filas = [fila for fila in csv.reader(io.StringIO(texto), delimiter=separador) if fila]
    except csv.Error:
        raise ArchivoInvalido("NO_PARSEABLE")   # p. ej. bytes nulos o comillas sin cerrar

    # [Prevención] Nombres sin espacios ni mayúsculas: " Kwh" se reconoce como "kwh".
    encabezado = [c.strip().lower() for c in filas[0]]
    col_hora, hora_en_utc, col_valor, col_unidad = identificar_columnas(encabezado)

    datos = filas[1:]
    if not datos:
        raise ArchivoInvalido("SIN_FILAS")
    # [README A] Archivo truncado o corrupto: toda fila debe tener EXACTAMENTE
    # tantos campos como el encabezado. Si no, la estructura está rota y se
    # rechaza el archivo entero (los meses ya cargados no se tocan).
    if any(len(fila) < len(encabezado) for fila in datos):
        raise ArchivoInvalido("FILAS_INCOMPLETAS")
    if any(len(fila) > len(encabezado) for fila in datos):
        raise ArchivoInvalido("FILAS_CON_CAMPOS_DE_MAS")

    # Todo queda como texto: se convierte a mano, columna por columna (abajo).
    df = pd.DataFrame(datos, columns=encabezado)

    unidades = df[col_unidad] if col_unidad else pd.Series("kWh", index=df.index)
    return pd.DataFrame({
        "frontera_id": df["frontera_id"].str.strip(),
        "ts_medicion": convertir_hora(df[col_hora].str.strip(), hora_en_utc),
        "kwh": [convertir_valor(v, u) for v, u in zip(df[col_valor], unidades)],
        "version_lectura": [convertir_entero(v) for v in df["version_lectura"]],
        "calidad_lectura": df["calidad_lectura"].str.strip() if "calidad_lectura" in df.columns else "",
        "unidad": unidades.str.strip(),
        "ts_texto": df[col_hora],
        "valor_texto": df[col_valor],
        "version_texto": df["version_lectura"],
        "archivo_origen": os.path.basename(ruta),
        "fila_origen": range(1, len(df) + 1),
    })
