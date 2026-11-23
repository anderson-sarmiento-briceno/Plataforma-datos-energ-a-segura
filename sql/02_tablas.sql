-- ---------------------------------------------------------------------------
-- 02 · Tablas del modelo.
--
-- Convenciones:
--   - Texto siempre TEXT: CLI0033 trae una razón social de 1.632 caracteres;
--     un VARCHAR(255) haría fallar la carga.
--   - Energía y dinero en NUMERIC sin escala fija: exacto (nunca float) y sin
--     redondeos silenciosos; se redondea a 3 decimales solo en la salida.
--   - PII (Restringido-PII) en columnas BYTEA: llegan ya cifradas desde Python
--     (AES-GCM, Paso 7). La base nunca ve la llave ni el dato en claro.
--   - archivo_origen / fila_origen: trazabilidad de dónde salió cada dato.
--   - IF NOT EXISTS: make up es idempotente. Ojo: si se cambia una columna,
--     hay que correr make clean (no altera tablas que ya existen).
-- ---------------------------------------------------------------------------

-- ===================== core: dimensiones =====================

CREATE TABLE IF NOT EXISTS core.dim_cliente (
    cliente_id                   TEXT PRIMARY KEY,
    razon_social                 TEXT,          -- nombre de empresa: Confidencial, en claro, fuera de la vista
    nit_hash                     TEXT,          -- HMAC-SHA256 del NIT normalizado: permite joins sin ver el NIT
    nit_cifrado                  BYTEA,
    email_cifrado                BYTEA,
    celular_cifrado              BYTEA,
    direccion_cifrada            BYTEA,
    representante_legal_cifrado  BYTEA,
    ciudad                       TEXT,
    departamento                 TEXT,
    observaciones_enmascarada    TEXT,          -- correos/celulares/cédulas reemplazados por máscaras
    observaciones_cifrada        BYTEA,         -- el texto original, cifrado: no se pierde nada
    archivo_origen               TEXT NOT NULL,
    cargado_en                   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_dim_cliente_nit_hash ON core.dim_cliente (nit_hash);

CREATE TABLE IF NOT EXISTS core.dim_frontera (
    frontera_id     TEXT PRIMARY KEY,
    codigo_sic      TEXT,
    cliente_id      TEXT NOT NULL REFERENCES core.dim_cliente (cliente_id),
    mercado         TEXT,
    operador_red    TEXT,
    nivel_tension   SMALLINT,
    fecha_registro  DATE,
    estado          TEXT,
    archivo_origen  TEXT NOT NULL,
    cargado_en      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS core.dim_limite_frontera (
    frontera_id     TEXT PRIMARY KEY REFERENCES core.dim_frontera (frontera_id),
    min_kwh         NUMERIC NOT NULL,
    max_kwh         NUMERIC NOT NULL,
    archivo_origen  TEXT NOT NULL,
    cargado_en      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (min_kwh < max_kwh)
);

-- ===================== core: hechos =====================

-- Una fila por frontera y hora. La PK es la llave de B3: si llega otra lectura
-- de la misma hora, el pipeline decide cuál gana con ON CONFLICT (Paso 5).
CREATE TABLE IF NOT EXISTS core.fact_consumo_horario (
    frontera_id      TEXT NOT NULL REFERENCES core.dim_frontera (frontera_id),
    ts_medicion      TIMESTAMPTZ NOT NULL,      -- instante exacto; el mes local se calcula en la vista (B1)
    kwh              NUMERIC NOT NULL,
    version_lectura  INTEGER NOT NULL,
    calidad_lectura  TEXT,                      -- OK / CORREGIDA: solo trazabilidad, no es regla
    archivo_origen   TEXT NOT NULL,
    fila_origen      INTEGER NOT NULL,
    cargado_en       TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (frontera_id, ts_medicion),
    -- Red de seguridad de B4: el pipeline ya filtra, pero si algo se escapa,
    -- la base lo rechaza en vez de guardarlo.
    CHECK (kwh >= 0 AND kwh <= 1000000)
);

CREATE TABLE IF NOT EXISTS core.fact_factura (
    factura_id                 TEXT PRIMARY KEY,   -- único en los 4 meses (verificado)
    frontera_id                TEXT NOT NULL REFERENCES core.dim_frontera (frontera_id),
    periodo                    TEXT NOT NULL,      -- 'YYYY-MM'
    nit_hash                   TEXT,               -- mismo HMAC que dim_cliente: se cruzan sin ver el NIT
    email_facturacion_cifrado  BYTEA,
    consumo_facturado_kwh      NUMERIC,
    tarifa_cop_kwh             NUMERIC,
    valor_total_cop            NUMERIC,
    metodo_pago_tipo           TEXT,
    cuenta_enmascarada         TEXT,               -- ya viene enmascarada (****9999)
    archivo_origen             TEXT NOT NULL,
    cargado_en                 TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS core.fact_factura_componente (
    factura_id     TEXT NOT NULL REFERENCES core.fact_factura (factura_id),
    concepto       TEXT NOT NULL,                  -- G, T, D, C, P, R
    valor_cop_kwh  NUMERIC NOT NULL,
    PRIMARY KEY (factura_id, concepto)
);

-- ===================== cuarentena (sin PII) =====================

-- Consumos rechazados: la fila tal como vino (texto) + motivo.
-- Motivos: NULO, NEGATIVO, OUTLIER, NO_NUMERICO, FRONTERA_DESCONOCIDA,
--          HORA_INVALIDA, DUPLICADO_EXACTO, DUPLICADO_CONFLICTIVO.
CREATE TABLE IF NOT EXISTS cuarentena.consumo_rechazado (
    archivo_origen  TEXT NOT NULL,
    fila_origen     INTEGER NOT NULL,
    motivo          TEXT NOT NULL,
    frontera_id     TEXT,
    ts_texto        TEXT,
    valor_texto     TEXT,
    unidad          TEXT,
    version_texto   TEXT,
    registrado_en   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (archivo_origen, fila_origen)       -- una fila del archivo, un rechazo: re-correr no duplica
);

-- Facturas cuya frontera no existe (B6). Solo IDs: nunca NIT ni email.
CREATE TABLE IF NOT EXISTS cuarentena.factura_huerfana (
    archivo_origen  TEXT NOT NULL,
    factura_id      TEXT NOT NULL,
    frontera_id     TEXT,
    periodo         TEXT,
    motivo          TEXT NOT NULL,
    registrado_en   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (archivo_origen, factura_id)
);

-- Clientes que no se pudieron cargar (B7: ninguno se pierde en silencio). Sin PII.
CREATE TABLE IF NOT EXISTS cuarentena.cliente_rechazado (
    archivo_origen  TEXT NOT NULL,
    fila_origen     INTEGER NOT NULL,
    cliente_id      TEXT,
    motivo          TEXT NOT NULL,
    registrado_en   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (archivo_origen, fila_origen)
);

-- Archivos rechazados enteros (truncados, corruptos, encabezado desconocido).
CREATE TABLE IF NOT EXISTS cuarentena.archivo_rechazado (
    archivo         TEXT NOT NULL,
    hash_sha256     TEXT NOT NULL,
    motivo          TEXT NOT NULL,
    registrado_en   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (archivo, hash_sha256)
);

-- ===================== control =====================

-- Un registro por archivo procesado. Si el hash no cambió, el archivo se
-- salta; los conteos guardados permiten armar dq_report.json aunque el
-- archivo no se vuelva a leer.
-- tipo (Paso 7): también se registran maestros y facturación, porque su PII
-- se cifra con un nonce aleatorio: recargarlos cambiaría los bytes cifrados
-- en cada corrida y rompería la idempotencia. Con el hash, se saltan.
CREATE TABLE IF NOT EXISTS control.archivo_procesado (
    archivo                  TEXT PRIMARY KEY,
    tipo                     TEXT NOT NULL DEFAULT 'consumo',   -- consumo / maestro / facturacion
    hash_sha256              TEXT NOT NULL,
    estado                   TEXT NOT NULL,      -- CARGADO / RECHAZADO
    filas_leidas             INTEGER NOT NULL DEFAULT 0,
    filas_cargadas           INTEGER NOT NULL DEFAULT 0,
    nulos                    INTEGER NOT NULL DEFAULT 0,
    negativos                INTEGER NOT NULL DEFAULT 0,
    outliers                 INTEGER NOT NULL DEFAULT 0,
    mwh_convertidos          INTEGER NOT NULL DEFAULT 0,
    duplicados_exactos       INTEGER NOT NULL DEFAULT 0,
    duplicados_conflictivos  INTEGER NOT NULL DEFAULT 0,
    correcciones_aplicadas   INTEGER NOT NULL DEFAULT 0,
    procesado_en             TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ===================== audit =====================

-- Una fila por etapa de cada corrida: quién, qué, cuándo, cuántas filas,
-- resultado. Solo el TIPO de error, nunca el mensaje (puede traer datos).
-- REVOKE UPDATE/DELETE + trigger que lo impide: Paso 7 (05_auditoria.sql).
CREATE TABLE IF NOT EXISTS audit.bitacora_corridas (
    id                BIGSERIAL PRIMARY KEY,
    corrida_id        TEXT NOT NULL,
    usuario_bd        TEXT NOT NULL DEFAULT current_user,
    etapa             TEXT NOT NULL,
    inicio            TIMESTAMPTZ NOT NULL,
    fin               TIMESTAMPTZ,
    filas_leidas      INTEGER,
    filas_cargadas    INTEGER,
    filas_rechazadas  INTEGER,
    resultado         TEXT NOT NULL,        -- OK / ERROR
    tipo_error        TEXT
);
