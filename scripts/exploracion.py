"""
Exploración inicial de los datos (Paso 2).

Abre cada archivo de data/ y cuenta lo que importa para diseñar el pipeline:
formatos, vacíos, negativos, outliers, duplicados, PII, huérfanos.
Al lado de cada cifra muestra lo que esperábamos (contexto.md §3), para ver
de inmediato qué cuadra y qué no.

Regla de seguridad: este script SOLO imprime conteos, nombres de columnas y
valores de columnas categóricas (versión, calidad, estado). Nunca imprime
NIT, correos, celulares, cédulas ni texto libre de clientes.

Uso:  .venv/bin/python scripts/exploracion.py
"""
import csv
import glob
import json
import os
import re

import pandas as pd

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(RAIZ, "data")

# Cifras de la exploración anterior (contexto.md §3). None = no había cifra.
ESPERADO = {
    "consumos_2026-01.csv": {"filas": 29685, "sobrantes": 79},
    "consumos_2026-02.csv": {"filas": 26855, "sobrantes": 63},
    "consumos_2026-03.csv": {"filas": 29718, "sobrantes": 83, "mwh": 400},
    "consumos_2026-04.csv": {"filas": 28782, "sobrantes": 75},
    "consumos_2026-04_correccion.csv": {"filas": 300},
}
LIMITE_OUTLIER_KWH = 1_000_000


def comparar(nombre, encontrado, esperado=None):
    """Imprime una cifra y, si hay valor esperado, si cuadra o no."""
    if esperado is None:
        print(f"    {nombre:<34} {encontrado}")
    else:
        marca = "ok" if encontrado == esperado else "DIFERENTE"
        print(f"    {nombre:<34} {encontrado}  (esperado {esperado}: {marca})")


def detectar_encoding(ruta):
    """Devuelve 'utf-8' si el archivo se decodifica así; si no, 'latin-1'.

    Latin-1 nunca falla (cada byte es un carácter), por eso va de segundo.
    """
    with open(ruta, "rb") as f:
        contenido = f.read()
    try:
        contenido.decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError:
        return "latin-1"


def detectar_separador(ruta, encoding):
    """Cuenta ';' y ',' en el encabezado: gana el que más aparezca."""
    with open(ruta, encoding=encoding) as f:
        encabezado = f.readline()
    return ";" if encabezado.count(";") > encabezado.count(",") else ","


def contar_crlf(ruta):
    """Cuántas líneas terminan en \\r\\n (saltos de línea de Windows)."""
    with open(ruta, "rb") as f:
        return f.read().count(b"\r\n")


def leer_csv_texto(ruta, encoding, separador):
    """Lee todo como texto (dtype=str) para no perder nada en conversiones.

    keep_default_na=False: un vacío queda como "" y no como NaN, así se
    cuenta igual que en el archivo.
    """
    return pd.read_csv(ruta, sep=separador, encoding=encoding, dtype=str,
                       keep_default_na=False)


def explorar_consumo(ruta, fronteras_maestro):
    """Cuenta formato, defectos y duplicados de un archivo de consumos."""
    nombre = os.path.basename(ruta)
    esperado = ESPERADO.get(nombre, {})
    encoding = detectar_encoding(ruta)
    separador = detectar_separador(ruta, encoding)
    df = leer_csv_texto(ruta, encoding, separador)

    col_hora = "timestamp_utc" if "timestamp_utc" in df.columns else "timestamp"
    col_valor = "kwh" if "kwh" in df.columns else "consumo_kwh"

    print(f"\n== {nombre}")
    comparar("encoding / separador", f"{encoding} / '{separador}'")
    comparar("líneas con CRLF", contar_crlf(ruta))
    comparar("columnas", list(df.columns))
    comparar("filas", len(df), esperado.get("filas"))
    comparar("rango de hora (texto)", f"{df[col_hora].min()} .. {df[col_hora].max()}")

    # Valor: coma decimal → punto. Para contar basta float; el pipeline usará Decimal.
    texto = df[col_valor].str.strip().str.replace(",", ".", regex=False)
    numero = pd.to_numeric(texto, errors="coerce")
    vacios = texto.eq("")
    no_numericos = numero.isna() & ~vacios

    # Unidad: solo marzo trae la columna; MWh se lleva a kWh antes de comparar.
    if "unidad" in df.columns:
        comparar("valores de 'unidad'", df["unidad"].value_counts().to_dict())
        es_mwh = df["unidad"].str.strip().str.lower().eq("mwh")
        comparar("filas en MWh", int(es_mwh.sum()), esperado.get("mwh"))
        numero = numero.where(~es_mwh, numero * 1000)

    comparar("vacíos", int(vacios.sum()))
    comparar("no numéricos (no vacíos)", int(no_numericos.sum()))
    comparar("negativos", int((numero < 0).sum()))
    comparar(f"outliers (> {LIMITE_OUTLIER_KWH:,} kWh)", int((numero > LIMITE_OUTLIER_KWH).sum()))
    comparar("valor máximo (kWh)", f"{numero.max():,.3f}")

    # Duplicados: filas sobrantes = filas - llaves únicas.
    llave = ["frontera_id", col_hora]
    sobrantes = len(df) - len(df.drop_duplicates(subset=llave))
    exactos = int(df.duplicated().sum())  # fila idéntica en TODAS las columnas
    comparar("duplicados sobrantes (llave)", sobrantes, esperado.get("sobrantes"))
    comparar("  exactos (fila idéntica)", exactos)
    comparar("  conflictivos (resto)", sobrantes - exactos)

    # Ambigüedad §8.1: ¿algún grupo duplicado mezcla un valor válido con uno inválido?
    invalido = vacios | no_numericos | (numero < 0) | (numero > LIMITE_OUTLIER_KWH)
    en_grupo_dup = df.duplicated(subset=llave, keep=False)
    llaves_con_invalido = set(map(tuple, df.loc[en_grupo_dup & invalido, llave].values))
    comparar("grupos duplicados con algún inválido", len(llaves_con_invalido), 0)

    comparar("valores de version_lectura", df["version_lectura"].value_counts().to_dict())
    comparar("valores de calidad_lectura", df["calidad_lectura"].value_counts().to_dict())

    ids = set(df["frontera_id"])
    comparar("fronteras distintas", len(ids))
    comparar("fronteras fuera del maestro", len(ids - fronteras_maestro))
    return df, col_hora


def explorar_correccion(df_correccion, df_abril):
    """Cuántas filas de la corrección pisan una llave que ya existía en abril."""
    llaves_abril = set(zip(df_abril["frontera_id"], df_abril["timestamp"]))
    llaves_corr = list(zip(df_correccion["frontera_id"], df_correccion["timestamp"]))
    pisan = sum(1 for k in llaves_corr if k in llaves_abril)
    print("\n== corrección vs abril")
    comparar("filas de corrección que pisan abril", pisan)
    comparar("filas de corrección nuevas", len(llaves_corr) - pisan)

    # Ambigüedad §8.4: ¿la corrección reemplaza lecturas válidas o rellena inválidas?
    valor_abril = pd.to_numeric(df_abril["consumo_kwh"], errors="coerce")
    abril_invalida = valor_abril.isna() | (valor_abril < 0) | (valor_abril > LIMITE_OUTLIER_KWH)
    llaves_invalidas = set(zip(df_abril.loc[abril_invalida, "frontera_id"],
                               df_abril.loc[abril_invalida, "timestamp"]))
    comparar("pisan una lectura inválida de abril", sum(1 for k in llaves_corr if k in llaves_invalidas))
    llaves_dup = set(zip(*[df_abril.loc[df_abril.duplicated(["frontera_id", "timestamp"], keep=False), c]
                           for c in ("frontera_id", "timestamp")]))
    comparar("pisan una llave duplicada en abril", sum(1 for k in llaves_corr if k in llaves_dup))


def clasificar_nit(nit):
    """Devuelve el FORMATO del NIT (no el NIT): así se cuenta sin exponerlo."""
    nit = nit.strip()
    if re.fullmatch(r"\d{3}\.\d{3}\.\d{3}-\d", nit):
        return "999.999.999-9"
    if re.fullmatch(r"\d{9}-\d", nit):
        return "999999999-9"
    if re.fullmatch(r"NIT\s*\d{9}-\d", nit, re.IGNORECASE):
        return "NIT 999999999-9"
    if re.fullmatch(r"\d{10}", nit):
        return "9999999999"
    return "otro"


# Patrones de PII en texto libre. Solo se usan para CONTAR clientes afectados.
PATRON_CORREO = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
PATRON_CELULAR = re.compile(r"(?<!\d)3\d{9}(?!\d)")
PATRON_CEDULA = re.compile(r"\bCC\.?\s*\d[\d.]{5,}", re.IGNORECASE)


def explorar_clientes(ruta):
    """Cuenta filas, formatos de NIT, PII en observaciones y entradas hostiles."""
    print(f"\n== {os.path.basename(ruta)}")
    encoding = detectar_encoding(ruta)
    with open(ruta, encoding=encoding) as f:
        lineas_fisicas = sum(1 for _ in f)
    # El parser CSV de pandas respeta comillas: un salto de línea dentro de un
    # campo entre comillas NO parte la fila (split(",") sí la partiría).
    df = leer_csv_texto(ruta, encoding, ",")
    comparar("encoding", encoding, "latin-1")
    comparar("líneas físicas (con encabezado)", lineas_fisicas, 42)
    comparar("clientes (filas parseadas)", len(df), 40)
    comparar("cliente_id distintos", df["cliente_id"].nunique(), 40)
    comparar("formatos de NIT", df["nit"].map(clasificar_nit).value_counts().to_dict())
    comparar("razón social más larga (caracteres)", int(df["razon_social"].str.len().max()), 1632)

    texto_fila = df.apply(lambda fila: " ".join(fila), axis=1)
    comparar("filas con texto tipo SQL (DROP/;--)", int(texto_fila.str.contains(r"DROP\s+TABLE|;--", case=False).sum()), 1)
    comparar("filas con salto de línea en un campo", int(texto_fila.str.contains("\n").sum()), 1)
    comparar("filas con comillas dentro de un campo", int(texto_fila.str.contains('"').sum()))

    obs = df["observaciones"]
    con_correo = obs.str.contains(PATRON_CORREO)
    con_celular = obs.str.contains(PATRON_CELULAR)
    con_cedula = obs.str.contains(PATRON_CEDULA)
    comparar("observaciones con correo", int(con_correo.sum()))
    comparar("observaciones con celular", int(con_celular.sum()))
    comparar("observaciones con cédula (CC)", int(con_cedula.sum()))
    comparar("clientes con PII en observaciones", int((con_correo | con_celular | con_cedula).sum()), 25)
    comparar("columnas", list(df.columns))
    return set(df["cliente_id"])


def explorar_fronteras(ruta, clientes):
    """Cuenta fronteras, sus estados y si apuntan a clientes que existen."""
    print(f"\n== {os.path.basename(ruta)}")
    df = leer_csv_texto(ruta, detectar_encoding(ruta), ",")
    comparar("fronteras", len(df), 40)
    comparar("frontera_id distintos", df["frontera_id"].nunique(), 40)
    comparar("valores de estado", df["estado"].value_counts().to_dict())
    comparar("valores de mercado", df["mercado"].value_counts().to_dict())
    comparar("fecha_registro mín .. máx", f"{df['fecha_registro'].min()} .. {df['fecha_registro'].max()}")
    comparar("registradas en 2026", int(df["fecha_registro"].str.startswith("2026").sum()))
    comparar("cliente_id fuera del maestro", int((~df["cliente_id"].isin(clientes)).sum()), 0)
    return set(df["frontera_id"])


def explorar_limites(ruta, fronteras):
    """Revisa que cada frontera tenga un límite coherente (mín < máx)."""
    print(f"\n== {os.path.basename(ruta)}")
    df = leer_csv_texto(ruta, detectar_encoding(ruta), ",")
    minimo = pd.to_numeric(df["min_kwh"], errors="coerce")
    maximo = pd.to_numeric(df["max_kwh"], errors="coerce")
    comparar("filas", len(df), 40)
    comparar("con mín >= máx o no numéricos", int((~(minimo < maximo)).sum()), 0)
    comparar("fronteras fuera del maestro", int((~df["frontera_id"].isin(fronteras)).sum()), 0)
    return dict(zip(df["frontera_id"], maximo))


def explorar_facturacion(fronteras):
    """Cuenta facturas, huérfanas y componentes por archivo."""
    for ruta in sorted(glob.glob(os.path.join(DATA, "facturacion", "*.json"))):
        with open(ruta, encoding="utf-8") as f:
            doc = json.load(f)
        facturas = doc["facturas"]
        huerfanas = [x for x in facturas if x["frontera_id"] not in fronteras]
        print(f"\n== {os.path.basename(ruta)}  (periodo {doc['periodo']})")
        comparar("facturas", len(facturas), 43)
        comparar("huérfanas (frontera fuera del maestro)", len(huerfanas), 3)
        comparar("huérfanas sin componentes", sum(1 for x in huerfanas if not x["componentes"]), 3)
        conceptos = sorted({c["concepto"] for x in facturas for c in x["componentes"]})
        comparar("conceptos de componentes", conceptos)
        comparar("métodos de pago", pd.Series([x["metodo_pago"]["tipo"] for x in facturas]).value_counts().to_dict())


def explorar_eventos(limite_max):
    """Cuenta eventos y simula las 3 reglas de alerta del streaming."""
    ruta = os.path.join(DATA, "eventos", "mediciones_stream.ndjson")
    with open(ruta, encoding="utf-8") as f:
        eventos = [json.loads(linea) for linea in f if linea.strip()]
    print(f"\n== {os.path.basename(ruta)}")
    comparar("eventos", len(eventos), 3795)
    comparar("evento_id distintos", len({e["evento_id"] for e in eventos}))
    comparar("fronteras distintas", len({e["frontera_id"] for e in eventos}), 40)
    comparar("kwh nulos", sum(1 for e in eventos if e["kwh"] is None))

    # Regla 1: fuera de banda = kwh > max_kwh (estricto).
    fuera_banda = sum(1 for e in eventos
                      if e["kwh"] is not None and e["kwh"] > limite_max.get(e["frontera_id"], float("inf")))
    comparar("fuera de banda (kwh > max_kwh)", fuera_banda, 22)

    # Regla 2: retroactiva = llega una hora menor que la máxima ya vista.
    maximo_visto, retroactivas = {}, 0
    for e in eventos:
        ts = pd.Timestamp(e["ts_medicion"])
        previo = maximo_visto.get(e["frontera_id"])
        if previo is not None and ts < previo:
            retroactivas += 1
        else:
            maximo_visto[e["frontera_id"]] = ts
    comparar("retroactivas", retroactivas, 5)

    # Regla 3: 3 intervalos de 15 min seguidos sin dato → una alerta por racha.
    rejilla = pd.date_range("2026-06-01T00:00:00-05:00", periods=96, freq="15min")
    vistos = {(e["frontera_id"], pd.Timestamp(e["ts_medicion"])) for e in eventos}
    caidas = 0
    for frontera in {e["frontera_id"] for e in eventos}:
        seguidos = 0
        for ts in rejilla:
            if (frontera, ts) in vistos:
                seguidos = 0
            else:
                seguidos += 1
                if seguidos == 3:  # solo al llegar a 3: no se repite en la misma racha
                    caidas += 1
    comparar("caídas (3 intervalos sin reporte)", caidas, 6)


def main():
    """Recorre todos los archivos de data/ en el orden en que los usaría el pipeline."""
    clientes = explorar_clientes(os.path.join(DATA, "maestros", "clientes.csv"))
    fronteras = explorar_fronteras(os.path.join(DATA, "maestros", "fronteras.csv"), clientes)
    limite_max = explorar_limites(os.path.join(DATA, "maestros", "limites_frontera.csv"), fronteras)

    leidos = {}
    for ruta in sorted(glob.glob(os.path.join(DATA, "consumos", "*.csv"))):
        leidos[os.path.basename(ruta)] = explorar_consumo(ruta, fronteras)[0]
    if "consumos_2026-04_correccion.csv" in leidos and "consumos_2026-04.csv" in leidos:
        explorar_correccion(leidos["consumos_2026-04_correccion.csv"], leidos["consumos_2026-04.csv"])

    explorar_facturacion(fronteras)
    explorar_eventos(limite_max)


if __name__ == "__main__":
    main()
