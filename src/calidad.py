"""
Reglas de calidad B3 y B4 (Paso 5), como funciones PURAS: reciben datos y
devuelven datos, sin base de datos. Así se prueban solas (tests/test_calidad.py),
igual que las reglas RC1–RC10 de agente-ordenes-compra/src/controles.ts.

Orden de aplicación (ADR-004): primero B4 (inválidos), después B3 (duplicados).
Una lectura inválida NO compite como duplicado: si una corrección llega
negativa, la lectura válida anterior se conserva en vez de dejar un hueco.

Origen de cada decisión:
  [Exploración]  lo encontró scripts/exploracion.py en los datos reales.
  [README]       lo exige una regla del README (B1–B7, C1–C8, D, E).
  [Prevención]   no está en los datos ni en el README; evita una falla concreta.
"""
from decimal import Decimal

import pandas as pd

from src.lectores import FACTOR_A_KWH

# [README B4] "valor > 1.000.000 kWh (outlier físicamente imposible)".
LIMITE_OUTLIER_KWH = Decimal("1000000")

# La llave de B3: una lectura por frontera y hora.
LLAVE = ["frontera_id", "ts_medicion"]

# Columnas "tal como vinieron": dos filas son un duplicado EXACTO si son
# idénticas en todas ellas [Exploración: así se contaron 180 exactos].
COLUMNAS_ORIGINALES = ["frontera_id", "ts_texto", "valor_texto", "unidad",
                       "version_texto", "calidad_lectura"]


def motivo_invalido(fila, fronteras_validas):
    """Devuelve el motivo por el que la fila es inválida, o None si es válida.

    Primero los motivos de B4 (los que cuenta el dq_report), después los demás.
      NULO                 [README B4] valor vacío
      UNIDAD_DESCONOCIDA   [README B2] unidad que no se sabe convertir
      NO_NUMERICO          [Prevención] texto que no es un número ("abc", "NaN")
      NEGATIVO             [README B4]
      OUTLIER              [README B4] > 1.000.000 kWh
      HORA_INVALIDA        [Prevención] hora vacía o en un formato no visto
      FRONTERA_DESCONOCIDA [Prevención] medidor que no está en el maestro (la FK lo rechazaría)
      VERSION_INVALIDA     [Prevención] sin versión no se puede aplicar B3
    """
    if fila.kwh is None:
        if not fila.valor_texto.strip():
            return "NULO"
        if fila.unidad.strip().lower() not in FACTOR_A_KWH:
            return "UNIDAD_DESCONOCIDA"
        return "NO_NUMERICO"
    if fila.kwh < 0:
        return "NEGATIVO"
    if fila.kwh > LIMITE_OUTLIER_KWH:
        return "OUTLIER"
    if pd.isna(fila.ts_medicion):
        return "HORA_INVALIDA"
    if fila.frontera_id not in fronteras_validas:
        return "FRONTERA_DESCONOCIDA"
    if fila.version_lectura is None or pd.isna(fila.version_lectura):
        return "VERSION_INVALIDA"
    return None


def separar_invalidos(df, fronteras_validas):
    """Aplica B4: devuelve (validas, rechazadas). Las rechazadas llevan columna 'motivo'.

    Ninguna fila se pierde: len(validas) + len(rechazadas) == len(df).
    """
    motivos = [motivo_invalido(fila, fronteras_validas) for fila in df.itertuples()]
    df = df.assign(motivo=motivos)
    return df[df["motivo"].isna()].drop(columns="motivo"), df[df["motivo"].notna()]


def resolver_duplicados(validas):
    """Aplica B3 dentro de UN archivo: devuelve (ganadoras, sobrantes con 'motivo').

    [README B3] Para una misma (frontera, hora) gana la versión mayor; si empatan,
    la última en orden de archivo (aquí, dentro del mismo archivo: la fila mayor).
    Se ordena y se queda la ÚLTIMA de cada llave (drop_duplicates keep="last").

    Sobrantes: [README] se cuentan FILAS sobrantes, no grupos.
      DUPLICADO_EXACTO       idéntica en todo a otra fila posterior del grupo
      DUPLICADO_CONFLICTIVO  misma llave pero algún valor distinto
    Ejemplo: grupo A, A, B (B gana) → sobrantes A (exacto) y A (conflictivo con B).
    Así exactos = filas − variantes distintas, y conflictivos = variantes − 1.
    """
    ordenadas = validas.sort_values(LLAVE + ["version_lectura", "fila_origen"])
    es_ganadora = ~ordenadas.duplicated(subset=LLAVE, keep="last")
    # keep="last": la última copia idéntica no se marca; las anteriores sí son "exactas".
    es_exacta = ordenadas.duplicated(subset=COLUMNAS_ORIGINALES, keep="last")
    sobrantes = ordenadas[~es_ganadora].assign(
        motivo=["DUPLICADO_EXACTO" if e else "DUPLICADO_CONFLICTIVO" for e in es_exacta[~es_ganadora]])
    return ordenadas[es_ganadora], sobrantes


def gana_nueva(nueva, existente):
    """B3 entre archivos: ¿la lectura nueva reemplaza a la que ya está en la base?

    Cada lectura es una tupla (version_lectura, archivo_origen, fila_origen).
    Python compara tuplas en orden: primero la versión; si empata, el nombre del
    archivo (orden de bytes, como COLLATE "C" en Postgres); si empata, la fila.
    >= y no >: si es exactamente la misma lectura, se vuelve a escribir igual en
    vez de rechazarla. (Un archivo que llega OTRA VEZ con contenido distinto no
    se compara consigo mismo: el pipeline borra antes lo que venía de él.)
    """
    return nueva >= existente


def resolver_contra_existentes(ganadoras, existentes):
    """B3 contra lo que ya está en la base (p. ej. la corrección de abril).

    `existentes`: DataFrame con frontera_id, ts_medicion, version_lectura,
    archivo_origen, fila_origen de las lecturas ya cargadas en ese rango.
    Devuelve (a_cargar, superadas con 'motivo', correcciones).
      correcciones = filas que reemplazan una lectura de OTRO archivo
      (contexto §8.4: la lectura más literal de "correcciones tardías").
    Motivo de las que pierden (revisión /code-review: antes todas decían
    "versión mayor", aunque perdieran por empate):
      SUPERADA_POR_VERSION_MAYOR  la ya cargada tiene versión mayor
      SUPERADA_POR_DESEMPATE      misma versión; gana el archivo o la fila posterior
    """
    if existentes.empty:
        return ganadoras, ganadoras.iloc[0:0].assign(motivo=[]), 0
    # Se compara en UTC: el mismo instante, sin importar en qué zona venga escrito.
    izquierda = ganadoras.assign(ts_utc=pd.to_datetime(ganadoras["ts_medicion"], utc=True))
    derecha = existentes.assign(ts_utc=pd.to_datetime(existentes["ts_medicion"], utc=True))
    cruce = izquierda.merge(
        derecha[["frontera_id", "ts_utc", "version_lectura", "archivo_origen", "fila_origen"]],
        on=["frontera_id", "ts_utc"], how="left", suffixes=("", "_bd"))

    hay_existente = cruce["archivo_origen_bd"].notna()
    gana = [True if not hay else gana_nueva(
                (f.version_lectura, f.archivo_origen, f.fila_origen),
                (int(f.version_lectura_bd), f.archivo_origen_bd, int(f.fila_origen_bd)))
            for f, hay in zip(cruce.itertuples(), hay_existente)]
    gana = pd.Series(gana, index=cruce.index)
    es_correccion = gana & hay_existente & (cruce["archivo_origen_bd"] != cruce["archivo_origen"])

    columnas = list(ganadoras.columns)
    a_cargar = cruce.loc[gana, columnas]
    perdedoras = cruce.loc[~gana]
    superadas = perdedoras[columnas].assign(motivo=[
        "SUPERADA_POR_VERSION_MAYOR" if f.version_lectura < f.version_lectura_bd
        else "SUPERADA_POR_DESEMPATE"
        for f in perdedoras.itertuples()])
    return a_cargar, superadas, int(es_correccion.sum())


def contar(rechazadas, sobrantes, validas):
    """Conteos de un archivo para control.archivo_procesado y el dq_report."""
    motivos = rechazadas["motivo"].value_counts()
    return {
        "nulos": int(motivos.get("NULO", 0)),
        "negativos": int(motivos.get("NEGATIVO", 0)),
        "outliers": int(motivos.get("OUTLIER", 0)),
        # [README] registros_mwh_convertidos: filas válidas que venían en MWh.
        "mwh_convertidos": int((validas["unidad"].str.strip().str.lower() == "mwh").sum()),
        "duplicados_exactos": int((sobrantes["motivo"] == "DUPLICADO_EXACTO").sum()),
        "duplicados_conflictivos": int((sobrantes["motivo"] == "DUPLICADO_CONFLICTIVO").sum()),
    }
