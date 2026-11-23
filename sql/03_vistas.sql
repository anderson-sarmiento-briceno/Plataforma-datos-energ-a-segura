-- ---------------------------------------------------------------------------
-- 03 · Vistas analíticas (sin PII).
--
-- v_consumo_mensual tiene exactamente las columnas de out/consumo_mensual.csv:
-- el analista y el archivo de salida ven lo mismo, calculado en un solo lugar.
--
-- B1: el mes se calcula en hora de Bogotá de forma EXPLÍCITA
-- (AT TIME ZONE 'America/Bogota'), para no depender de la zona horaria
-- configurada en el servidor ni en la sesión.
-- Solo se cargan lecturas válidas, así que COUNT(*) = horas con dato.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE VIEW analytics.v_consumo_mensual AS
SELECT
    c.frontera_id,
    to_char(c.ts_medicion AT TIME ZONE 'America/Bogota', 'YYYY-MM') AS mes,
    ROUND(SUM(c.kwh), 3)                                            AS consumo_kwh,
    COUNT(*)                                                        AS horas_con_dato
FROM core.fact_consumo_horario AS c
GROUP BY c.frontera_id, mes;
