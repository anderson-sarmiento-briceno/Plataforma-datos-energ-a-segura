"""
Configuración del pipeline (mismo patrón que Detector-de-placas/src/config.py).

  - config/pipeline.yaml → configuración NO sensible (rutas, tamaño de lote).
  - .env                 → credenciales. Nunca se escriben en código ni en el YAML.
"""
import os

import yaml
from dotenv import load_dotenv

# Raíz del proyecto: las rutas se arman desde aquí, no desde la carpeta en la
# que se ejecute el comando (en etl-dw-prototype las rutas estaban escritas a mano).
RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def cargar_config(ruta=None):
    """Lee config/pipeline.yaml y carga las variables de .env al entorno.

    yaml.safe_load y no yaml.load: safe_load solo crea datos simples (dict,
    list, str…); load puede ejecutar código escondido en el YAML (el CVE de
    PyYAML que traía el prototipo inicial).
    load_dotenv no pisa variables que ya existan en el entorno.
    """
    load_dotenv(os.path.join(RAIZ, ".env"))
    with open(ruta or os.path.join(RAIZ, "config", "pipeline.yaml"), encoding="utf-8") as f:
        return yaml.safe_load(f)


def ruta_datos(config):
    """Carpeta raíz de los datos (por defecto data/), relativa a la raíz del proyecto."""
    return os.path.join(RAIZ, config["origen"]["carpeta_datos"])
