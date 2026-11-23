"""
C2 · PII en texto libre (Paso 7).

[README C2] El campo `observaciones` tiene correos, celulares y cédulas que
alguien escribió donde no debía. Se detectan y se enmascaran, y se cuenta
cuántos clientes estaban afectados (dq_report: clientes_con_pii_en_texto_libre).

[Exploración] Formas encontradas (sin imprimir valores): cédulas "CC 99999999",
celulares de 10 dígitos que empiezan por 3, correos normales. 14 correos,
11 celulares y 6 cédulas en 25 clientes. No hay otros números largos.

Las mismas expresiones sirven de red de seguridad para los logs (Paso 8).
"""
import re

# Cédula: "CC", punto opcional, espacios, y 6 o más dígitos (con o sin puntos).
PATRON_CEDULA = re.compile(r"\b(CC\.?\s*)(\d[\d.]{5,}\d)", re.IGNORECASE)
# Celular colombiano: 3 + 9 dígitos, SIN dígitos ni letras pegados antes, ni
# dígitos después. Así no se confunde con un pedazo de un número más largo ni
# con un código de frontera: "CO3001957318" tiene 10 dígitos que empiezan por 3,
# pero lleva letras delante (verificado al volcar la base: sin la regla de la
# letra, el patrón "encontraba" 14.300 celulares que eran códigos de frontera).
PATRON_CELULAR = re.compile(r"(?<![0-9A-Za-z])3\d{9}(?!\d)")
# Correo: usuario@dominio.terminación
PATRON_CORREO = re.compile(r"([\w.+-])[\w.+-]*@([\w-]+(?:\.[\w-]+)+)")


def _mascara_cedula(m):
    """CC 12345678 → CC ******78 (se ve que había una cédula, no cuál)."""
    digitos = re.sub(r"\D", "", m.group(2))
    return m.group(1) + "*" * (len(digitos) - 2) + digitos[-2:]


def _mascara_celular(m):
    """3001234567 → 3*******67"""
    numero = m.group(0)
    return numero[0] + "*" * 7 + numero[-2:]


def _mascara_correo(m):
    """juan.perez@empresa.com.co → j***@empresa.com.co (el dominio sirve para saber de qué empresa es)."""
    return f"{m.group(1)}***@{m.group(2)}"


def enmascarar(texto):
    """Devuelve (texto enmascarado, True si tenía PII).

    Orden: primero cédulas (traen "CC" delante), luego celulares, luego correos.
    Las máscaras ya no cumplen los patrones (tienen *), así que enmascarar dos
    veces da el mismo resultado y una segunda búsqueda encuentra 0.
    """
    if not texto:
        return texto, False
    resultado = PATRON_CEDULA.sub(_mascara_cedula, texto)
    resultado = PATRON_CELULAR.sub(_mascara_celular, resultado)
    resultado = PATRON_CORREO.sub(_mascara_correo, resultado)
    return resultado, resultado != texto


def tiene_pii(texto):
    """True si el texto contiene un correo, un celular o una cédula."""
    return bool(texto) and any(p.search(texto) for p in (PATRON_CEDULA, PATRON_CELULAR, PATRON_CORREO))
