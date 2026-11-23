"""
C3 · Cifrado de PII a nivel de aplicación (Paso 7).

  - AES-256-GCM para guardar la PII: cifrado SIMÉTRICO (la misma llave cifra
    y descifra) y AUTENTICADO (si alguien altera un byte, descifrar falla en
    vez de devolver basura).
  - HMAC-SHA256 del NIT normalizado (nit_hash): una "huella con llave". El
    mismo NIT siempre da la misma huella, así que facturas y clientes se cruzan
    sin ver el NIT; sin la llave no se puede calcular ni revertir.

Por qué en la aplicación y no en Postgres (pgcrypto): la llave NUNCA viaja a
la base. Quien tenga un dump o acceso de DBA solo ve bytes (verificado leyendo
la tabla como superusuario). En producción la llave vendría de un KMS
(envelope encryption, ver infra/).

Las dos llaves salen de .env (las genera `make setup`, al azar, y no van a git).
Son llaves DISTINTAS: una para cifrar y otra para el HMAC (separación de llaves).
"""
import base64
import hashlib
import hmac
import os
import re

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

TAMANO_NONCE = 12   # bytes; el tamaño recomendado para GCM


def cargar_llave(nombre):
    """Lee una llave de 32 bytes (256 bits) en base64 desde la variable de entorno `nombre`."""
    valor = os.getenv(nombre)
    if not valor:
        raise RuntimeError(f"Falta {nombre} en .env (correr make setup, que la genera)")
    llave = base64.b64decode(valor)
    if len(llave) != 32:
        raise RuntimeError(f"{nombre} debe tener 32 bytes (256 bits)")
    return llave


def cifrar(texto, llave):
    """Cifra un texto con AES-256-GCM. Devuelve nonce + texto cifrado (bytes), o None si está vacío.

    El nonce es un número aleatorio que se usa UNA sola vez por cifrado: por eso
    el mismo texto cifrado dos veces da bytes distintos (nadie puede saber que
    dos clientes tienen el mismo correo). Se guarda pegado al inicio porque hace
    falta para descifrar; no es secreto.
    """
    if texto is None or not texto.strip():
        return None
    nonce = os.urandom(TAMANO_NONCE)
    return nonce + AESGCM(llave).encrypt(nonce, texto.encode("utf-8"), None)


def descifrar(datos, llave):
    """Inverso de cifrar(). Si los bytes fueron alterados o la llave no es la correcta, lanza InvalidTag."""
    if datos is None:
        return None
    datos = bytes(datos)
    return AESGCM(llave).decrypt(datos[:TAMANO_NONCE], datos[TAMANO_NONCE:], None).decode("utf-8")


def normalizar_nit(nit):
    """Deja solo los dígitos: "900.123.456-7", "NIT 900123456-7" y "9001234567" quedan iguales.

    [Exploración] El NIT viene en 4 formatos; normalizado son 10 dígitos en
    clientes y en facturas.
    """
    return re.sub(r"\D", "", nit or "")


def huella_nit(nit, llave_hmac):
    """HMAC-SHA256 (hex) del NIT normalizado, o None si no hay dígitos."""
    digitos = normalizar_nit(nit)
    if not digitos:
        return None
    return hmac.new(llave_hmac, digitos.encode("utf-8"), hashlib.sha256).hexdigest()
