"""
Logs estructurados en JSON (Paso 8, Parte E).

[README E] Logs estructurados en JSON, una línea por evento, en
logs/pipeline.log, sin PII: ni correos, ni celulares, ni cédulas.

Dos capas contra la PII (defensa en profundidad):
  1. Por diseño: el pipeline solo registra conteos, nombres de archivo, etapas
     y el TIPO de los errores. Nunca filas ni valores.
  2. Red de seguridad: cada línea, ya armada, pasa por src/pii.enmascarar()
     antes de escribirse. Si alguien agregara un log descuidado, el correo, el
     celular o la cédula saldrían tachados.

Uso:
    log.info("consumos", extra={"datos": {"archivo": "...", "filas_leidas": 29685}})
→   {"ts": "2026-…Z", "nivel": "INFO", "evento": "consumos", "archivo": "...", "filas_leidas": 29685}
"""
import json
import logging
import os
from datetime import datetime, timezone

from src.pii import enmascarar


class FormatoJson(logging.Formatter):
    """Convierte cada evento en UNA línea JSON y le aplica el filtro anti-PII."""

    def format(self, record):
        evento = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(timespec="milliseconds"),
            "nivel": record.levelname,
            "evento": record.getMessage(),
            **getattr(record, "datos", {}),
        }
        # default=str: Decimal, fechas, etc. se escriben como texto en vez de fallar.
        linea = json.dumps(evento, ensure_ascii=False, default=str)
        linea_segura, _ = enmascarar(linea)   # capa 2: red de seguridad
        return linea_segura


def configurar_logs(carpeta):
    """Envía los logs a <carpeta>/pipeline.log y a la consola, ambos en JSON.

    Se limpian los manejadores anteriores para no duplicar líneas si main()
    se llama más de una vez (por ejemplo, en las pruebas).
    """
    os.makedirs(carpeta, exist_ok=True)
    raiz = logging.getLogger()
    for manejador in list(raiz.handlers):
        raiz.removeHandler(manejador)
    for manejador in (logging.FileHandler(os.path.join(carpeta, "pipeline.log"), encoding="utf-8"),
                      logging.StreamHandler()):
        manejador.setFormatter(FormatoJson())
        raiz.addHandler(manejador)
    raiz.setLevel(logging.INFO)
    # La librería kafka-python escribe en INFO cada conexión y cada paso del
    # protocolo (algunos con la palabra "Error" aunque sean normales, como
    # MemberIdRequiredError al entrar a un grupo). De ella solo interesan los
    # avisos y errores reales.
    logging.getLogger("kafka").setLevel(logging.WARNING)
