"""
Archivos de salida (Paso 8). Formato exacto en el README (sección Salidas).

    out/consumo_mensual.csv   frontera_id,mes,consumo_kwh,horas_con_dato
    out/dq_report.json        las 12 llaves de calidad
    out/metrics.json          filas leídas/cargadas/rechazadas por motivo, duración por etapa

Dos ideas clave:
  1. Todo sale de lo GUARDADO en la base, no de lo procesado en esta corrida:
     la 2.ª corrida de make run salta todos los archivos, y los informes deben
     dar exactamente lo mismo ([README A] idempotencia).
  2. consumo_mensual.csv se lee con el rol ANALISTA (ADR-005): ese rol solo ve
     la vista sin PII, así que el CSV no puede contener PII aunque alguien
     cambiara la consulta: Postgres la rechazaría.
"""
import calendar
import csv
import json
import os

COLUMNAS_CSV = ["frontera_id", "mes", "consumo_kwh", "horas_con_dato"]


def horas_del_mes(mes):
    """Horas de un mes 'YYYY-MM' en Bogotá: días × 24.

    [README B1] Bogotá es UTC−5 fijo, sin horario de verano: todos los días
    tienen 24 horas (enero 744, febrero 672, marzo 744, abril 720).
    """
    anio, numero = (int(x) for x in mes.split("-"))
    return calendar.monthrange(anio, numero)[1] * 24


def leer_consumo_mensual(conexion_analista):
    """Filas de analytics.v_consumo_mensual, ordenadas por frontera y mes (con el rol analista)."""
    with conexion_analista.cursor() as cursor:
        cursor.execute("""
            SELECT frontera_id, mes, consumo_kwh, horas_con_dato
            FROM analytics.v_consumo_mensual
            ORDER BY frontera_id, mes""")
        filas = cursor.fetchall()
    conexion_analista.rollback()
    return filas


def escribir_consumo_mensual(filas, ruta):
    """Escribe el CSV con kWh a 3 decimales exactos (Decimal → texto, sin pasar por float)."""
    with open(ruta, "w", encoding="utf-8", newline="") as f:
        escritor = csv.writer(f, lineterminator="\n")
        escritor.writerow(COLUMNAS_CSV)
        for frontera, mes, consumo, horas in filas:
            escritor.writerow([frontera, mes, f"{consumo:.3f}", horas])


def calcular_huecos(filas_mensuales, fronteras, meses):
    """B5: horas sin lectura válida, por frontera (suma de todos los meses).

    [README B5] "Una hora sin lectura válida es un hueco, da igual si la fila no
    existía o si existía pero fue descartada por B4". Por eso se recorren TODAS
    las fronteras del maestro en TODOS los meses con datos: una frontera que no
    reportó nada en un mes suma las horas completas de ese mes (contexto §8.3).
    Los huecos se reportan, no se rellenan.
    """
    con_dato = {(frontera, mes): horas for frontera, mes, _, horas in filas_mensuales}
    return {frontera: sum(horas_del_mes(mes) - con_dato.get((frontera, mes), 0) for mes in meses)
            for frontera in sorted(fronteras)}


def armar_dq_report(cursor, filas_mensuales, clientes_con_pii):
    """Las 12 llaves del README, todas presentes aunque valgan 0.

    Los conteos de consumos se suman de control.archivo_procesado (lo que se
    guardó al cargar cada archivo), así cuadran aunque esta corrida se salte todo.
    """
    cursor.execute("""
        SELECT COALESCE(SUM(filas_leidas), 0), COALESCE(SUM(duplicados_exactos), 0),
               COALESCE(SUM(duplicados_conflictivos), 0), COALESCE(SUM(negativos), 0),
               COALESCE(SUM(nulos), 0), COALESCE(SUM(outliers), 0), COALESCE(SUM(mwh_convertidos), 0),
               COALESCE(SUM(correcciones_aplicadas), 0)
        FROM control.archivo_procesado
        WHERE tipo = 'consumo' AND estado = 'CARGADO'""")
    (leidas, exactos, conflictivos, negativos, nulos, outliers, mwh, correcciones) = cursor.fetchone()

    cursor.execute("SELECT frontera_id FROM core.dim_frontera")
    fronteras = [fila[0] for fila in cursor.fetchall()]
    meses = sorted({mes for _, mes, _, _ in filas_mensuales})
    huecos = calcular_huecos(filas_mensuales, fronteras, meses)

    # [README B6] fronteras huérfanas DISTINTAS (12 facturas de 12 fronteras: da igual; Paso 6).
    cursor.execute("SELECT count(DISTINCT frontera_id) FROM cuarentena.factura_huerfana")
    huerfanas = cursor.fetchone()[0]

    return {
        "filas_leidas": int(leidas),
        "duplicados_exactos": int(exactos),
        "duplicados_conflictivos": int(conflictivos),
        "registros_negativos": int(negativos),
        "registros_nulos": int(nulos),
        "registros_outlier": int(outliers),
        "registros_mwh_convertidos": int(mwh),
        "correcciones_tardias_aplicadas": int(correcciones),
        "horas_sin_lectura_valida_total": sum(huecos.values()),
        "horas_sin_lectura_valida_por_frontera": huecos,
        "fronteras_huerfanas_facturacion": int(huerfanas),
        "clientes_con_pii_en_texto_libre": int(clientes_con_pii),
    }


def armar_metrics(cursor, corrida_id, inicio, fin, etapas):
    """[README E] filas leídas, cargadas, rechazadas por motivo, duración por etapa y timestamp.

    Dos bloques:
      corrida    lo que hizo ESTA corrida (en la 2.ª, casi todo en 0 porque se salta).
      acumulado  el estado de la base: lo que ya está cargado y en cuarentena.
    """
    cursor.execute("""
        SELECT 'consumo:' || motivo, count(*) FROM cuarentena.consumo_rechazado GROUP BY motivo
        UNION ALL SELECT 'factura:' || motivo, count(*) FROM cuarentena.factura_huerfana GROUP BY motivo
        UNION ALL SELECT 'cliente:' || motivo, count(*) FROM cuarentena.cliente_rechazado GROUP BY motivo
        UNION ALL SELECT 'archivo:' || motivo, count(*) FROM cuarentena.archivo_rechazado GROUP BY motivo""")
    rechazadas_por_motivo = {motivo: int(n) for motivo, n in sorted(cursor.fetchall())}
    cursor.execute("""
        SELECT (SELECT COALESCE(SUM(filas_leidas), 0) FROM control.archivo_procesado WHERE tipo = 'consumo'),
               (SELECT count(*) FROM core.fact_consumo_horario),
               (SELECT count(*) FROM core.fact_factura)""")
    leidas, consumos, facturas = cursor.fetchone()
    return {
        "corrida_id": corrida_id,
        "timestamp_inicio": inicio.isoformat(timespec="seconds"),
        "timestamp_fin": fin.isoformat(timespec="seconds"),
        "duracion_total_s": round((fin - inicio).total_seconds(), 3),
        "corrida": {
            "filas_leidas": sum(e["filas_leidas"] for e in etapas),
            "filas_cargadas": sum(e["filas_cargadas"] for e in etapas),
            "filas_rechazadas": sum(e["filas_rechazadas"] for e in etapas),
            "etapas": etapas,   # duración y filas de cada etapa
        },
        "acumulado": {
            "filas_leidas_consumos": int(leidas),
            "filas_cargadas_consumos": int(consumos),
            "facturas_cargadas": int(facturas),
            "filas_rechazadas_por_motivo": rechazadas_por_motivo,
        },
    }


def escribir_json(datos, ruta):
    """JSON legible (indentado), en UTF-8."""
    with open(ruta, "w", encoding="utf-8") as f:
        json.dump(datos, f, ensure_ascii=False, indent=2)
        f.write("\n")


def generar_salidas(conexion, conexion_analista, carpeta_out, clientes_con_pii):
    """Escribe consumo_mensual.csv y dq_report.json. Devuelve el dq_report (para el log)."""
    os.makedirs(carpeta_out, exist_ok=True)
    filas = leer_consumo_mensual(conexion_analista)
    escribir_consumo_mensual(filas, os.path.join(carpeta_out, "consumo_mensual.csv"))
    with conexion.cursor() as cursor:
        reporte = armar_dq_report(cursor, filas, clientes_con_pii)
    conexion.rollback()
    escribir_json(reporte, os.path.join(carpeta_out, "dq_report.json"))
    return filas, reporte
