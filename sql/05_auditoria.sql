-- ---------------------------------------------------------------------------
-- 05 · Bitácora append-only (C5).
--
-- [README C5] UPDATE y DELETE revocados de verdad, no por convención.
-- Dos capas (defensa en profundidad):
--   1. Permisos: nadie tiene UPDATE, DELETE ni TRUNCATE (04 solo da INSERT a
--      ingesta y SELECT a auditor; aquí se revoca explícitamente a todos).
--   2. Trigger: aunque alguien tuviera el permiso (incluso el dueño o un
--      superusuario), cualquier UPDATE, DELETE o TRUNCATE lanza un error.
-- Límite conocido: un superusuario podría DESACTIVAR el trigger; en
-- producción la bitácora se copiaría además a un almacenamiento inmutable
-- (p. ej. S3 con Object Lock o CloudTrail). Va en THREAT_MODEL.md.
-- ---------------------------------------------------------------------------

\getenv rol_ingesta  PG_ROL_INGESTA_USER
\getenv rol_analista PG_ROL_ANALISTA_USER
\getenv rol_auditor  PG_ROL_AUDITOR_USER

-- Capa 1: permisos
REVOKE UPDATE, DELETE, TRUNCATE ON audit.bitacora_corridas
    FROM PUBLIC, :"rol_ingesta", :"rol_analista", :"rol_auditor";

-- Capa 2: trigger que bloquea cualquier cambio. TG_OP dice qué se intentó.
CREATE OR REPLACE FUNCTION audit.bloquear_cambios()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'audit.bitacora_corridas es append-only: % no está permitido', TG_OP;
END;
$$;
REVOKE ALL ON FUNCTION audit.bloquear_cambios() FROM PUBLIC;

-- Por fila: UPDATE y DELETE. Por sentencia: TRUNCATE (no pasa fila por fila).
DROP TRIGGER IF EXISTS tr_bitacora_sin_cambios ON audit.bitacora_corridas;
CREATE TRIGGER tr_bitacora_sin_cambios
    BEFORE UPDATE OR DELETE ON audit.bitacora_corridas
    FOR EACH ROW EXECUTE FUNCTION audit.bloquear_cambios();

DROP TRIGGER IF EXISTS tr_bitacora_sin_truncate ON audit.bitacora_corridas;
CREATE TRIGGER tr_bitacora_sin_truncate
    BEFORE TRUNCATE ON audit.bitacora_corridas
    FOR EACH STATEMENT EXECUTE FUNCTION audit.bloquear_cambios();
