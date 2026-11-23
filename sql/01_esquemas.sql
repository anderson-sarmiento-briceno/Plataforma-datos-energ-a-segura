-- ---------------------------------------------------------------------------
-- 01 · Esquemas: una "zona" por tipo de dato.
-- Separar en esquemas permite dar permisos por zona en el Paso 7
-- (p. ej. el analista solo ve "analytics").
-- IF NOT EXISTS: make up se puede correr N veces sin error.
-- ---------------------------------------------------------------------------

-- Modelo dimensional limpio (dimensiones y hechos).
CREATE SCHEMA IF NOT EXISTS core;

-- Lo rechazado no se borra: queda aquí, consultable, con su motivo.
CREATE SCHEMA IF NOT EXISTS cuarentena;

-- Control de la carga incremental: qué archivo se procesó y con qué hash.
CREATE SCHEMA IF NOT EXISTS control;

-- Bitácora de corridas (append-only; la protección se agrega en el Paso 7).
CREATE SCHEMA IF NOT EXISTS audit;

-- Lo único que ve el rol analista: vistas sin PII.
CREATE SCHEMA IF NOT EXISTS analytics;
