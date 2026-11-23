"""
Pruebas de C4 (menor privilegio) y C5 (bitácora append-only), Paso 7.

[README C4] Se conectan con cada rol e intentan hacer lo que ese rol no
debería poder hacer. Si un rol puede más de lo que dice, la prueba falla.

Necesitan la base arriba (make up). Si el superusuario no puede conectarse
(base apagada), se SALTAN. Si la base está arriba pero un rol no puede
conectarse, FALLAN: eso sería un error real de configuración.
Todo se hace en transacciones que terminan en ROLLBACK: no queda nada guardado.
"""
import psycopg2
import pytest
from psycopg2 import errors

from src.bd import conectar
from src.cifrado import cargar_llave, descifrar
from src.config import cargar_config


@pytest.fixture(scope="module")
def roles():
    """Una conexión por rol. Se salta todo el módulo si la base no está arriba."""
    cargar_config()
    try:
        conectar("superusuario").close()
    except (psycopg2.OperationalError, RuntimeError):
        pytest.skip("Postgres no está arriba (correr make up)")
    conexiones = {rol: conectar(rol) for rol in ("ingesta", "analista", "auditor", "superusuario")}
    yield conexiones
    for conexion in conexiones.values():
        conexion.rollback()
        conexion.close()


def puede(conexion, sql):
    """True si el rol puede ejecutar la sentencia; False si Postgres la rechaza por permisos.

    Siempre deshace (ROLLBACK): la prueba no cambia nada.
    """
    try:
        with conexion.cursor() as cursor:
            cursor.execute(sql)
        return True
    except errors.InsufficientPrivilege:
        return False
    finally:
        conexion.rollback()


# ----------------------------- analista -----------------------------

def test_analista_solo_lee_la_vista(roles) -> None:
    analista = roles["analista"]
    assert puede(analista, "SELECT * FROM analytics.v_consumo_mensual LIMIT 1")
    assert not puede(analista, "SELECT * FROM core.dim_cliente LIMIT 1")          # PII en claro: nunca
    assert not puede(analista, "SELECT * FROM core.fact_consumo_horario LIMIT 1")
    assert not puede(analista, "SELECT * FROM cuarentena.consumo_rechazado LIMIT 1")
    assert not puede(analista, "SELECT * FROM audit.bitacora_corridas LIMIT 1")


def test_analista_no_escribe_nada(roles) -> None:
    analista = roles["analista"]
    assert not puede(analista, "DELETE FROM core.fact_consumo_horario")
    assert not puede(analista, "CREATE TABLE analytics.copia AS SELECT 1")
    assert not puede(analista, "CREATE TABLE public.copia (x int)")


def test_la_vista_del_analista_no_trae_pii(roles) -> None:
    with roles["analista"].cursor() as cursor:
        cursor.execute("SELECT * FROM analytics.v_consumo_mensual LIMIT 0")
        columnas = [c.name for c in cursor.description]
    roles["analista"].rollback()
    assert columnas == ["frontera_id", "mes", "consumo_kwh", "horas_con_dato"]


# ----------------------------- auditor -----------------------------

def test_auditor_solo_lee_la_bitacora(roles) -> None:
    auditor = roles["auditor"]
    assert puede(auditor, "SELECT * FROM audit.bitacora_corridas LIMIT 1")
    assert not puede(auditor, "SELECT * FROM core.dim_cliente LIMIT 1")
    assert not puede(auditor, "SELECT * FROM analytics.v_consumo_mensual LIMIT 1")
    assert not puede(auditor, "DELETE FROM audit.bitacora_corridas")
    assert not puede(auditor, "UPDATE audit.bitacora_corridas SET resultado = 'OK'")


# ----------------------------- ingesta -----------------------------

def test_ingesta_escribe_pero_no_borra_tablas_ni_toca_la_bitacora(roles) -> None:
    ingesta = roles["ingesta"]
    assert not puede(ingesta, "DROP TABLE core.fact_consumo_horario")             # no puede borrar los consumos
    assert not puede(ingesta, "TRUNCATE core.fact_consumo_horario")
    assert not puede(ingesta, "UPDATE audit.bitacora_corridas SET resultado = 'OK'")
    assert not puede(ingesta, "DELETE FROM audit.bitacora_corridas")
    assert not puede(ingesta, "SELECT * FROM audit.bitacora_corridas LIMIT 1")   # solo agrega
    assert not puede(ingesta, "SELECT * FROM analytics.v_consumo_mensual LIMIT 1")


def test_ingesta_ve_la_pii_solo_cifrada(roles) -> None:
    # "No lee PII en claro": puede leer la tabla, pero solo bytes cifrados.
    # Se descifra cada valor con la llave de .env (que NO está en la base) y se
    # comprueba que el texto original no aparece dentro de los bytes guardados.
    # (Buscar "@" en los bytes no sirve: son aleatorios y por azar alguno vale 64 = "@".)
    # Solo se cuentan resultados: la prueba nunca imprime un correo.
    llave = cargar_llave("LLAVE_CIFRADO")
    with roles["ingesta"].cursor() as cursor:
        cursor.execute("SELECT email_cifrado FROM core.dim_cliente WHERE email_cifrado IS NOT NULL")
        cifrados = [bytes(fila[0]) for fila in cursor.fetchall()]
    roles["ingesta"].rollback()
    originales = [descifrar(c, llave) for c in cifrados]
    son_correos = sum("@" in o for o in originales)
    en_claro = sum(o.encode("utf-8") in c for o, c in zip(originales, cifrados))
    assert len(cifrados) == 40 and son_correos == 40 and en_claro == 0


# ----------------------------- bitácora (C5) -----------------------------

def test_bitacora_append_only_incluso_para_el_superusuario(roles) -> None:
    # El trigger bloquea UPDATE, DELETE y TRUNCATE aunque se tenga el permiso.
    su = roles["superusuario"]
    for sql in ("UPDATE audit.bitacora_corridas SET resultado = 'X'",
                "DELETE FROM audit.bitacora_corridas",
                "TRUNCATE audit.bitacora_corridas"):
        with pytest.raises(errors.RaiseException):
            with su.cursor() as cursor:
                cursor.execute("INSERT INTO audit.bitacora_corridas (corrida_id, etapa, inicio, resultado) "
                               "VALUES ('prueba', 'prueba', now(), 'OK')")
                cursor.execute(sql)
        su.rollback()


def test_un_usuario_cualquiera_no_puede_conectarse(roles) -> None:
    # Se le quitó CONNECT a PUBLIC: un rol nuevo sin permisos no entra a la base.
    su = roles["superusuario"]
    with su.cursor() as cursor:
        cursor.execute("SELECT has_database_privilege('public', current_database(), 'CONNECT')")
        assert cursor.fetchone()[0] is False
    su.rollback()
