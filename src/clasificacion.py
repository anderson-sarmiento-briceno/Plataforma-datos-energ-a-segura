"""
C1 · Lee data_classification.yaml (Paso 7).

El pipeline consulta este archivo para decidir QUÉ se cifra y DE DÓNDE sale
el dato: así la clasificación es un artefacto vivo, no un documento muerto
([README C1]).
"""
import os

import yaml

from src.config import RAIZ

NIVEL_PII = "Restringido-PII"


def cargar_clasificacion(ruta=None):
    """Devuelve {tabla: {columna: {"nivel": ..., "origen": ... o None}}}.

    En el YAML una columna puede escribirse corta (`ciudad: Interno`) o larga
    (`nit_cifrado: {nivel: Restringido-PII, origen: nit}`); aquí se dejan
    todas en la forma larga. Si un nivel no existe, falla: un error de tipeo
    en la clasificación no puede pasar en silencio.
    """
    with open(ruta or os.path.join(RAIZ, "data_classification.yaml"), encoding="utf-8") as f:
        documento = yaml.safe_load(f)
    niveles = set(documento["niveles"])
    clasificacion = {}
    for tabla, columnas in documento["tablas"].items():
        clasificacion[tabla] = {}
        for columna, valor in columnas.items():
            regla = valor if isinstance(valor, dict) else {"nivel": valor}
            if regla["nivel"] not in niveles:
                raise ValueError(f"Nivel desconocido en {tabla}.{columna}: {regla['nivel']}")
            clasificacion[tabla][columna] = {"nivel": regla["nivel"], "origen": regla.get("origen")}
    return clasificacion


def columnas_a_cifrar(clasificacion, tabla):
    """{columna destino: columna de origen} de las columnas Restringido-PII de una tabla.

    Ej.: core.dim_cliente → {"nit_cifrado": "nit", "email_cifrado": "email_contacto", ...}
    """
    return {columna: regla["origen"]
            for columna, regla in clasificacion.get(tabla, {}).items()
            if regla["nivel"] == NIVEL_PII and regla["origen"]}
