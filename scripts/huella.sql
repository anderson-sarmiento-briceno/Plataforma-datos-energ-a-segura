-- ---------------------------------------------------------------------------
-- Huella (md5) del estado de las tablas que escribe el pipeline.
-- Sirve para comprobar la IDEMPOTENCIA: correr esto, `make run`, y correrlo
-- otra vez; si las huellas son iguales, la segunda corrida no cambió nada.
--
--   docker compose exec -T postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At' < scripts/huella.sql
--
-- Solo cuenta filas y resume con md5: no imprime ningún dato.
-- Incluye cargado_en / registrado_en / procesado_en: si algo se reescribiera,
-- aunque fuera con el mismo valor, la fecha cambiaría y la huella también.
-- ---------------------------------------------------------------------------
SELECT 'fact_consumo_horario', count(*),
       md5(string_agg(frontera_id || '|' || ts_medicion || '|' || kwh || '|' || version_lectura || '|' ||
                      archivo_origen || '|' || fila_origen || '|' || cargado_en, ',' ORDER BY frontera_id, ts_medicion))
FROM core.fact_consumo_horario
UNION ALL
SELECT 'consumo_rechazado', count(*),
       md5(string_agg(archivo_origen || '|' || fila_origen || '|' || motivo || '|' || registrado_en, ','
                      ORDER BY archivo_origen, fila_origen))
FROM cuarentena.consumo_rechazado
UNION ALL
SELECT 'archivo_procesado', count(*),
       md5(string_agg(archivo || '|' || hash_sha256 || '|' || procesado_en, ',' ORDER BY archivo))
FROM control.archivo_procesado
UNION ALL
-- Facturación (Paso 6): se recarga con upsert en cada corrida; la huella usa
-- los valores (no cargado_en) porque el upsert reescribe las mismas filas.
SELECT 'fact_factura', count(*),
       md5(string_agg(factura_id || '|' || frontera_id || '|' || periodo || '|' ||
                      coalesce(valor_total_cop::text, ''), ',' ORDER BY factura_id))
FROM core.fact_factura
UNION ALL
SELECT 'fact_factura_componente', count(*),
       md5(string_agg(factura_id || '|' || concepto || '|' || valor_cop_kwh, ',' ORDER BY factura_id, concepto))
FROM core.fact_factura_componente
UNION ALL
SELECT 'factura_huerfana', count(*),
       md5(string_agg(archivo_origen || '|' || factura_id || '|' || registrado_en, ',' ORDER BY archivo_origen, factura_id))
FROM cuarentena.factura_huerfana;
