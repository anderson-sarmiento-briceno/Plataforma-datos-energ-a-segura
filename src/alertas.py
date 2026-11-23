"""
Reglas de alerta del streaming (Paso 9, Parte D), como funciones PURAS.

Reciben una lista de mediciones (en el orden en que llegaron) y devuelven
alertas. No saben nada de Kafka: src/stream_consumidor.py solo las alimenta.
Así se prueban en milisegundos con mediciones inventadas (tests/test_alertas.py),
igual que las reglas de calidad del batch.

Las tres reglas, exactas (README, sección D):
  CONSUMO_FUERA_DE_BANDA  kwh ESTRICTAMENTE mayor que max_kwh de la frontera
                          (limites_frontera.csv). ts = el de la medición.
  LECTURA_RETROACTIVA     llega una medición con ts_medicion ANTERIOR a la última
                          ya procesada de esa frontera. ts = el de la que llegó tarde.
  FRONTERA_SIN_REPORTE    3 intervalos consecutivos de 15 min sin medición.
                          Una sola alerta por caída. ts = el del TERCER intervalo.
La rejilla del día son 96 intervalos de 15 min desde 2026-06-01T00:00:00-05:00.

[Exploración] Simuladas sobre el archivo real: 22 + 5 + 6 = 33 alertas.
"""
import csv
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

ZONA = ZoneInfo("America/Bogota")
INICIO_REJILLA = datetime(2026, 6, 1, 0, 0, tzinfo=ZONA)   # [README D]
INTERVALOS_DEL_DIA = 96
PASO = timedelta(minutes=15)
INTERVALOS_PARA_CAIDA = 3                                  # 3 × 15 min = 45 min


def leer_hora(texto):
    """Texto ISO 8601 con zona → instante en hora de Bogotá. Sin zona → ValueError.

    Se exige la zona: "10:00" sin offset sería ambiguo. "Z" (UTC) se acepta
    también en Python < 3.11, por eso se reemplaza a mano.
    """
    instante = datetime.fromisoformat(texto.replace("Z", "+00:00"))
    if instante.tzinfo is None:
        raise ValueError("hora sin zona horaria")
    return instante.astimezone(ZONA)


def formato_ts(instante):
    """[README] ts en ISO 8601 con offset: 2026-06-01T06:30:00-05:00."""
    return instante.astimezone(ZONA).isoformat()


def validar_medicion(crudo):
    """Convierte un mensaje (dict) en una medición {frontera_id, ts, kwh}, o None si no sirve.

    [Prevención] Un mensaje roto (sin frontera, sin hora, hora ilegible) se
    descarta en vez de tumbar el consumidor. Si solo el kWh es ilegible, la
    medición cuenta como "reportó" (rejilla y retroactiva), pero no se evalúa la banda.
    kWh como Decimal: se compara exacto con el máximo del CSV.
    """
    if not isinstance(crudo, dict):
        return None
    frontera = crudo.get("frontera_id")
    if not isinstance(frontera, str) or not frontera.strip():
        return None
    try:
        ts = leer_hora(str(crudo.get("ts_medicion")))
    except ValueError:
        return None
    kwh = crudo.get("kwh")
    try:
        kwh = Decimal(str(kwh)) if isinstance(kwh, (int, float)) and not isinstance(kwh, bool) else None
    except InvalidOperation:
        kwh = None
    if kwh is not None and not kwh.is_finite():
        kwh = None
    return {"frontera_id": frontera.strip(), "ts": ts, "kwh": kwh}


def leer_limites(ruta):
    """{frontera_id: max_kwh} desde limites_frontera.csv ([README D])."""
    with open(ruta, encoding="utf-8") as f:
        return {fila["frontera_id"].strip(): Decimal(fila["max_kwh"]) for fila in csv.DictReader(f)}


def leer_fronteras(ruta):
    """Lista de frontera_id del maestro: la rejilla se revisa para TODAS, reporten o no."""
    with open(ruta, encoding="utf-8") as f:
        return [fila["frontera_id"].strip() for fila in csv.DictReader(f)]


def alertas_fuera_de_banda(mediciones, limites):
    """kwh > max_kwh (estricto: igual al máximo NO alerta)."""
    return [("CONSUMO_FUERA_DE_BANDA", m["frontera_id"], m["ts"])
            for m in mediciones
            if m["kwh"] is not None and m["frontera_id"] in limites
            and m["kwh"] > limites[m["frontera_id"]]]


def alertas_retroactivas(mediciones):
    """Hora anterior a la ÚLTIMA ya procesada de esa frontera, en orden de llegada.

    La medición atrasada NO cambia "la última": si la cambiara, la siguiente
    medición normal parecería atrasada también.
    """
    ultima = {}
    alertas = []
    for m in mediciones:
        previa = ultima.get(m["frontera_id"])
        if previa is not None and m["ts"] < previa:
            alertas.append(("LECTURA_RETROACTIVA", m["frontera_id"], m["ts"]))
        else:
            ultima[m["frontera_id"]] = m["ts"]
    return alertas


def alertas_sin_reporte(mediciones, fronteras):
    """Recorre la rejilla de 96 intervalos: al llegar a 3 vacíos seguidos, UNA alerta.

    `== 3` (no `>= 3`): una caída larga da una sola alerta. Cuando la frontera
    vuelve a reportar, el contador vuelve a 0 y una nueva caída alerta otra vez.
    """
    reportadas = {(m["frontera_id"], m["ts"]) for m in mediciones}
    rejilla = [INICIO_REJILLA + PASO * i for i in range(INTERVALOS_DEL_DIA)]
    alertas = []
    for frontera in fronteras:
        vacios_seguidos = 0
        for intervalo in rejilla:
            if (frontera, intervalo) in reportadas:
                vacios_seguidos = 0
            else:
                vacios_seguidos += 1
                if vacios_seguidos == INTERVALOS_PARA_CAIDA:
                    alertas.append(("FRONTERA_SIN_REPORTE", frontera, intervalo))
    return alertas


def calcular_alertas(mediciones, limites, fronteras):
    """Las 3 reglas, sin duplicados (sin alertas repetidas), en orden estable.

    Devuelve una lista de dicts {tipo, frontera_id, ts} listos para out/alertas.json.
    """
    todas = (alertas_fuera_de_banda(mediciones, limites) + alertas_retroactivas(mediciones)
             + alertas_sin_reporte(mediciones, fronteras))
    unicas = {(tipo, frontera, formato_ts(ts)) for tipo, frontera, ts in todas}
    return [{"tipo": tipo, "frontera_id": frontera, "ts": ts}
            for tipo, frontera, ts in sorted(unicas, key=lambda a: (a[2], a[0], a[1]))]
