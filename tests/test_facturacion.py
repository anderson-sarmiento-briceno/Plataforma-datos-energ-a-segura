"""
Pruebas de src/facturacion.py (Paso 6, B6). Sin base de datos: JSON inventados en tmp_path.
"""
from decimal import Decimal

import pytest

from src.facturacion import aplanar_factura, leer_facturacion, separar_huerfanas
from src.lectores import ArchivoInvalido


def factura(factura_id="F-1", frontera="CO1", componentes=(("G", "100.25"), ("T", "20.50"))):
    """Una factura con la misma forma que las del JSON real (sin datos de clientes)."""
    return {"factura_id": factura_id, "frontera_id": frontera,
            "consumo_facturado_kwh": Decimal("1500.5"), "tarifa_cop_kwh": Decimal("812.40"),
            "valor_total_cop": Decimal("1218606.20"),
            "componentes": [{"concepto": c, "valor_cop_kwh": Decimal(v)} for c, v in componentes],
            "metodo_pago": {"tipo": "pse", "cuenta_enmascarada": "****1234"}}


def test_los_numeros_se_leen_exactos(tmp_path) -> None:
    ruta = tmp_path / "facturacion_2026-01.json"
    ruta.write_text('{"periodo": "2026-01", "facturas": [{"factura_id": "F-1", "valor_total_cop": 1234.10}]}')
    periodo, facturas = leer_facturacion(ruta)
    assert periodo == "2026-01"
    assert facturas[0]["valor_total_cop"] == Decimal("1234.10")   # Decimal, no float


def test_aplanar_una_factura_en_fila_y_componentes() -> None:
    fila, componentes = aplanar_factura(factura(), "2026-01", "facturacion_2026-01.json")
    assert fila[0:3] == ("F-1", "CO1", "2026-01")
    assert fila[6:8] == ("pse", "****1234")
    assert componentes == [("F-1", "G", Decimal("100.25")), ("F-1", "T", Decimal("20.50"))]


def test_factura_sin_metodo_de_pago_no_rompe() -> None:
    sin_pago = factura()
    del sin_pago["metodo_pago"]
    fila, _ = aplanar_factura(sin_pago, "2026-01", "x.json")
    assert fila[6:8] == (None, None)


def test_separar_huerfanas() -> None:
    validas, huerfanas = separar_huerfanas([factura("F-1", "CO1"), factura("F-9001", "CO_NO_EXISTE")],
                                           {"CO1", "CO2"})
    assert [f["factura_id"] for f in validas] == ["F-1"]
    assert [f["factura_id"] for f in huerfanas] == ["F-9001"]


@pytest.mark.parametrize("contenido", ['{"periodo": "2026-01", "facturas": [', '{"periodo": "2026-01"}',
                                       '{"periodo": "2026-01", "facturas": "no es lista"}'])
def test_json_roto_o_incompleto_se_rechaza(tmp_path, contenido) -> None:
    ruta = tmp_path / "facturacion_mala.json"
    ruta.write_text(contenido)
    with pytest.raises(ArchivoInvalido) as error:
        leer_facturacion(ruta)
    assert error.value.motivo == "JSON_INVALIDO"
