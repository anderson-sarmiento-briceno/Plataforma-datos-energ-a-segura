"""
Pruebas de src/cifrado.py (Paso 7, C3). Llaves de prueba al azar: no se usan las de .env.
"""
import os

import pytest
from cryptography.exceptions import InvalidTag

from src.cifrado import cifrar, descifrar, huella_nit

LLAVE = os.urandom(32)
LLAVE_HMAC = os.urandom(32)


def test_cifrar_y_descifrar_devuelve_el_original() -> None:
    texto = "dato personal de prueba con tildes: Bogotá"
    assert descifrar(cifrar(texto, LLAVE), LLAVE) == texto


def test_el_texto_cifrado_no_contiene_el_original() -> None:
    cifrado = cifrar("correo.de.prueba@ejemplo.com", LLAVE)
    assert b"correo" not in cifrado and b"ejemplo" not in cifrado


def test_el_mismo_texto_da_bytes_distintos_cada_vez() -> None:
    # Por el nonce aleatorio: nadie puede saber que dos clientes tienen el mismo dato.
    assert cifrar("igual", LLAVE) != cifrar("igual", LLAVE)


def test_un_byte_alterado_se_detecta() -> None:
    # GCM es cifrado AUTENTICADO: si alguien cambia un byte, descifrar falla (no devuelve basura).
    cifrado = bytearray(cifrar("monto o dato importante", LLAVE))
    cifrado[-1] ^= 1
    with pytest.raises(InvalidTag):
        descifrar(bytes(cifrado), LLAVE)


def test_con_otra_llave_no_se_puede_descifrar() -> None:
    with pytest.raises(InvalidTag):
        descifrar(cifrar("secreto", LLAVE), os.urandom(32))


def test_vacio_no_se_cifra() -> None:
    assert cifrar("", LLAVE) is None and cifrar("   ", LLAVE) is None and cifrar(None, LLAVE) is None


def test_huella_del_nit_es_igual_en_los_4_formatos() -> None:
    # [Exploración] El NIT viene en 4 formatos; normalizado da la misma huella → el join funciona.
    formatos = ["900.123.456-7", "900123456-7", "NIT 900123456-7", "9001234567"]
    assert len({huella_nit(f, LLAVE_HMAC) for f in formatos}) == 1


def test_huella_depende_de_la_llave_y_no_revela_el_nit() -> None:
    huella = huella_nit("9001234567", LLAVE_HMAC)
    assert huella != huella_nit("9001234567", os.urandom(32))   # sin la llave no se puede calcular
    assert "9001234567" not in huella and len(huella) == 64
    assert huella_nit("", LLAVE_HMAC) is None
