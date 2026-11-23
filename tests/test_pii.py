"""
Pruebas de src/pii.py (Paso 7, C2). Datos INVENTADOS con la misma forma que los reales.
"""
from src.pii import enmascarar, tiene_pii


def test_enmascara_correo_celular_y_cedula() -> None:
    texto = "Enviar a juan.perez@empresa.com.co, llamar al 3001234567. Rep legal CC 12345678."
    resultado, tenia = enmascarar(texto)
    assert tenia
    assert "j***@empresa.com.co" in resultado      # el dominio se conserva (dice de qué empresa es)
    assert "3*******67" in resultado
    assert "CC ******78" in resultado
    assert not tiene_pii(resultado)                 # después de enmascarar no queda nada


def test_texto_sin_pii_no_cambia() -> None:
    texto = "Cliente con contrato vigente. Sin novedades. Revisar medidor en 30 días."
    assert enmascarar(texto) == (texto, False)


def test_enmascarar_dos_veces_da_lo_mismo() -> None:
    una, _ = enmascarar("Contacto: ana@correo.com tel 3109876543")
    dos, tenia = enmascarar(una)
    assert dos == una and not tenia


def test_no_confunde_un_numero_largo_con_un_celular() -> None:
    # 3001234567 dentro de un número de 13 dígitos no es un celular.
    assert not tiene_pii("Factura 3001234567890 pendiente")
    assert not tiene_pii("NIT 9001234567")          # el NIT de la empresa no es PII de persona
    # Un código de frontera (CO + 10 dígitos que empiezan por 3) no es un celular.
    assert not tiene_pii("Revisar frontera CO3001957318")
    assert tiene_pii("tel:3001957318")              # pero con ":" delante sí es un celular


def test_vacio() -> None:
    assert enmascarar("") == ("", False) and enmascarar(None) == (None, False)
