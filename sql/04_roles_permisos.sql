-- ---------------------------------------------------------------------------
-- 04 · Roles y permisos (C4 · menor privilegio).
--
-- Tres roles, cada uno con SOLO lo que necesita:
--   ingesta   escribe en el modelo, la cuarentena y el control; solo AGREGA a
--             la bitácora. No lee PII en claro: está cifrada y la llave no
--             está en la base.
--   analista  solo SELECT en la vista analytics.v_consumo_mensual (sin PII).
--   auditor   solo SELECT en audit.bitacora_corridas (sin PII).
-- Las tablas son del superusuario: ningún rol puede borrarlas ni alterarlas.
--
-- Usuarios y contraseñas NO están escritos aquí: `make up` los pasa como
-- variables de entorno al contenedor (sin ponerlas en la línea de comandos)
-- y \getenv las lee. :'x' las inserta como texto entre comillas y :"x" como
-- nombre (identificador) entre comillas: psql las escapa, no se arma SQL a mano.
--
-- Idempotente: los roles se crean solo si no existen; GRANT/REVOKE se pueden
-- repetir sin error.
-- ---------------------------------------------------------------------------

\getenv base           POSTGRES_DB
\getenv rol_ingesta    PG_ROL_INGESTA_USER
\getenv clave_ingesta  PG_ROL_INGESTA_PASSWORD
\getenv rol_analista   PG_ROL_ANALISTA_USER
\getenv clave_analista PG_ROL_ANALISTA_PASSWORD
\getenv rol_auditor    PG_ROL_AUDITOR_USER
\getenv clave_auditor  PG_ROL_AUDITOR_PASSWORD

-- ---------- Crear los roles (si no existen) ----------
-- \gexec ejecuta como SQL el resultado del SELECT: así el CREATE ROLE solo
-- ocurre cuando el WHERE NOT EXISTS encuentra que el rol no existe.
SELECT format('CREATE ROLE %I LOGIN', :'rol_ingesta')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'rol_ingesta') \gexec
SELECT format('CREATE ROLE %I LOGIN', :'rol_analista')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'rol_analista') \gexec
SELECT format('CREATE ROLE %I LOGIN', :'rol_auditor')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'rol_auditor') \gexec

-- Contraseña (se reaplica en cada make up, por si cambió en .env) y ningún
-- privilegio especial: no son superusuarios, no crean bases ni roles.
ALTER ROLE :"rol_ingesta"  WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD :'clave_ingesta';
ALTER ROLE :"rol_analista" WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD :'clave_analista';
ALTER ROLE :"rol_auditor"  WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD :'clave_auditor';

-- ---------- Quitarle todo a PUBLIC (= cualquier usuario) ----------
-- En Postgres, PUBLIC es "todos los roles". Por defecto puede conectarse a la
-- base y usar el esquema public: se le quita todo y se da solo lo necesario.
REVOKE ALL ON DATABASE :"base" FROM PUBLIC;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
REVOKE ALL ON SCHEMA core, cuarentena, control, audit, analytics FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA core, cuarentena, control, audit, analytics FROM PUBLIC;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA audit FROM PUBLIC;

GRANT CONNECT ON DATABASE :"base" TO :"rol_ingesta", :"rol_analista", :"rol_auditor";

-- ---------- ingesta ----------
GRANT USAGE ON SCHEMA core, cuarentena, control, audit TO :"rol_ingesta";
-- SELECT hace falta para el upsert (ON CONFLICT) y para leer lo ya cargado (B3 entre archivos).
GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA core TO :"rol_ingesta";
GRANT SELECT, INSERT ON ALL TABLES IN SCHEMA cuarentena TO :"rol_ingesta";
GRANT SELECT, INSERT, UPDATE ON control.archivo_procesado TO :"rol_ingesta";
-- DELETE solo donde lo necesita la recarga de un archivo que llegó cambiado (Paso 5).
GRANT DELETE ON core.fact_consumo_horario, cuarentena.consumo_rechazado TO :"rol_ingesta";
-- Bitácora: solo AGREGAR (ni leer, ni cambiar, ni borrar).
GRANT INSERT ON audit.bitacora_corridas TO :"rol_ingesta";
GRANT USAGE ON SEQUENCE audit.bitacora_corridas_id_seq TO :"rol_ingesta";

-- ---------- analista ----------
-- Solo la vista. Sin USAGE en core no puede ni ver las tablas base. La vista
-- funciona porque se ejecuta con los permisos de su dueño, no del analista.
GRANT USAGE ON SCHEMA analytics TO :"rol_analista";
GRANT SELECT ON analytics.v_consumo_mensual TO :"rol_analista";

-- ---------- auditor ----------
GRANT USAGE ON SCHEMA audit TO :"rol_auditor";
GRANT SELECT ON audit.bitacora_corridas TO :"rol_auditor";
