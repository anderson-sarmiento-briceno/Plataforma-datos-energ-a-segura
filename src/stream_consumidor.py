"""
Consumidor del streaming (Paso 9): python -m src.stream_consumidor   (lo llama `make stream`)

`make stream` hace tres cosas, en orden:
  1. python -m src.stream_consumidor --preparar-topico
       borra y crea de nuevo el tópico (si se corre dos veces, no hay mediciones repetidas)
  2. python tools/producer.py --rate 0
       el productor (tools/producer.py) pone las 3.795 mediciones en el tópico y termina
  3. python -m src.stream_consumidor
       este consumidor las lee todas, aplica las 3 reglas (src/alertas.py) y
       escribe out/alertas.json. Termina SOLO cuando pasan `espera_ms` sin
       mensajes nuevos (es un proceso por lotes: no se queda escuchando).

Kafka (Redpanda) es el servidor que guarda los mensajes en orden; este script
se conecta con la librería kafka-python, como pipeline.py se conecta a
Postgres con psycopg2.
"""
import argparse
import json
import logging
import os
import sys
import time
import uuid

from kafka import KafkaAdminClient, KafkaConsumer
from kafka.admin import NewTopic

from src.alertas import calcular_alertas, leer_fronteras, leer_limites, validar_medicion
from src.config import RAIZ, cargar_config, ruta_datos
from src.logs import configurar_logs
from src.salidas import escribir_json

log = logging.getLogger("stream")


def esperar(condicion, segundos=20):
    """Repite `condicion()` hasta que sea True o se acabe el tiempo (Kafka borra y crea en segundo plano)."""
    limite = time.monotonic() + segundos
    while time.monotonic() < limite:
        if condicion():
            return True
        time.sleep(0.5)
    return False


def preparar_topico(bootstrap, topico, particiones):
    """Borra el tópico si existe y lo crea vacío.

    [Prevención] Si `make stream` se corre dos veces sin recrearlo, el tópico
    tendría las mediciones repetidas y aparecerían alertas falsas.
    Particiones: con 1 se lee en el orden del archivo. El productor usa la
    frontera como llave, así que con más particiones el orden POR FRONTERA (lo
    que importa para la regla retroactiva) se mantendría igual.
    """
    admin = KafkaAdminClient(bootstrap_servers=bootstrap)
    try:
        if topico in admin.list_topics():
            admin.delete_topics([topico])
            if not esperar(lambda: topico not in admin.list_topics()):
                raise RuntimeError("el tópico no se borró a tiempo")
        admin.create_topics([NewTopic(name=topico, num_partitions=particiones, replication_factor=1)])
        if not esperar(lambda: topico in admin.list_topics()):
            raise RuntimeError("el tópico no se creó a tiempo")
    finally:
        admin.close()


def consumir(bootstrap, topico, espera_ms):
    """Lee TODOS los mensajes del tópico, en orden. Devuelve (mediciones válidas, mensajes descartados).

    - group_id NUEVO en cada corrida + auto_offset_reset="earliest": Kafka
      recuerda "por dónde iba" cada grupo (el offset); con un grupo nuevo se
      lee desde el primer mensaje.
    - enable_auto_commit=False: no hace falta guardar el avance; cada corrida
      relee todo y da el mismo resultado.
    - consumer_timeout_ms: si pasan `espera_ms` sin mensajes, el for termina.
    """
    consumidor = KafkaConsumer(
        topico,
        bootstrap_servers=bootstrap,
        group_id=f"alertas-{uuid.uuid4()}",
        auto_offset_reset="earliest",
        enable_auto_commit=False,
        consumer_timeout_ms=espera_ms,
    )
    mediciones, descartados = [], 0
    try:
        for mensaje in consumidor:
            try:
                medicion = validar_medicion(json.loads(mensaje.value))
            except (ValueError, UnicodeDecodeError):
                medicion = None
            if medicion is None:
                descartados += 1      # [Prevención] un mensaje roto no tumba el consumidor
            else:
                mediciones.append(medicion)
    finally:
        consumidor.close()
    return mediciones, descartados


def main(argumentos=None):
    """Prepara el tópico (--preparar-topico) o consume y escribe out/alertas.json. Devuelve el exit code."""
    parser = argparse.ArgumentParser(description="Consumidor de mediciones en streaming")
    parser.add_argument("--preparar-topico", action="store_true",
                        help="borrar y crear vacío el tópico (antes de correr el productor)")
    args = parser.parse_args(argumentos)

    config = cargar_config()
    configurar_logs(os.path.join(RAIZ, config["salidas"]["carpeta_logs"]))
    ajustes = config["stream"]
    bootstrap = os.getenv("KAFKA_BOOTSTRAP", "localhost:9092")
    topico = os.getenv("KAFKA_TOPIC", "mediciones.raw")
    try:
        if args.preparar_topico:
            preparar_topico(bootstrap, topico, ajustes["particiones"])
            log.info("stream_topico_preparado", extra={"datos": {"topico": topico}})
            return 0

        inicio = time.monotonic()
        mediciones, descartados = consumir(bootstrap, topico, ajustes["espera_ms"])
        maestros = os.path.join(ruta_datos(config), "maestros")
        alertas = calcular_alertas(mediciones, leer_limites(os.path.join(maestros, "limites_frontera.csv")),
                                   leer_fronteras(os.path.join(maestros, "fronteras.csv")))
        carpeta_out = os.path.join(RAIZ, config["salidas"]["carpeta_out"])
        os.makedirs(carpeta_out, exist_ok=True)
        escribir_json(alertas, os.path.join(carpeta_out, "alertas.json"))

        por_tipo = {}
        for alerta in alertas:
            por_tipo[alerta["tipo"]] = por_tipo.get(alerta["tipo"], 0) + 1
        log.info("stream_alertas", extra={"datos": {
            "mediciones": len(mediciones), "mensajes_descartados": descartados,
            "alertas": len(alertas), "alertas_por_tipo": por_tipo,
            "duracion_s": round(time.monotonic() - inicio, 3)}})
        return 0
    except Exception as error:
        # Solo el tipo de error (mismo criterio que el pipeline batch).
        log.error("stream_fallo", extra={"datos": {"tipo_error": type(error).__name__}})
        return 1


if __name__ == "__main__":
    sys.exit(main())
