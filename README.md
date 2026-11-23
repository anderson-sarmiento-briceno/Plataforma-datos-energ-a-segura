# Plataforma de datos de consumo energético

Plataforma de datos para los **consumos horarios de fronteras comerciales de energía** (Colombia).
Toma archivos crudos que llegan en formatos distintos cada mes, los limpia con reglas de calidad
exactas, los carga de forma **idempotente** en PostgreSQL y protege los datos personales de los
clientes con **cifrado a nivel de aplicación, roles de menor privilegio y una bitácora
append-only**. Un consumidor de Kafka (Redpanda) vigila las mediciones en vivo y emite alertas.

**Lista para producción:** transacciones por archivo, cuarentena consultable, logs
estructurados sin PII, threat model STRIDE mapeado a ISO/IEC 27001:2022 e infraestructura AWS
en Terraform (S3, KMS, IAM). Corre completa en local con Docker y `make`; para desplegarla en AWS
se aplican `infra/` y los endurecimientos de la §4.

**Stack:** Python 3.10+ (pandas, psycopg2, cryptography, kafka-python) · PostgreSQL 16 ·
Redpanda · Docker Compose · Terraform (AWS S3, KMS, IAM) · pytest.

---

## 1. Cómo correrlo

Requisitos: Linux (o WSL2), Docker con Compose v2, `make`, Python 3.10 o superior con `venv`.
Terraform solo para `make validar-infra` (opcional).

```bash
make setup      # .venv + dependencias; crea .env desde .env.example y genera las llaves de cifrado
make up         # Postgres + Redpanda (solo en 127.0.0.1), espera los healthchecks y aplica sql/*.sql
make run        # pipeline batch completo                      (~7 s desde cero)
make run        # otra vez: salta todo por hash, mismo estado  (~1 s)
make stream     # tópico nuevo + productor + consumidor        (~11 s, termina solo)
make test       # 72 pruebas
make down

make validar-infra  # terraform init -backend=false + validate + fmt -check
make clean          # down -v + borra out/ y logs/
```

> **Datos:** `data/` no está en el repositorio porque contiene datos personales. El pipeline
> espera `data/consumos/*.csv`, `data/maestros/{clientes,fronteras,limites_frontera}.csv`,
> `data/facturacion/*.json` y `data/eventos/mediciones_stream.ndjson`.

**Salidas:**

| Archivo | Comando | Contenido |
|---|---|---|
| `out/consumo_mensual.csv` | `make run` | `frontera_id,mes,consumo_kwh,horas_con_dato`, kWh con 3 decimales |
| `out/dq_report.json` | `make run` | Conteos de calidad (ver §3) |
| `out/metrics.json` | `make run` | Filas leídas, cargadas y rechazadas por motivo, duración por etapa |
| `logs/pipeline.log` | `make run` / `make stream` | Una línea JSON por evento, sin PII |
| `out/alertas.json` | `make stream` | Alertas sin duplicados, `ts` ISO 8601 con offset |

---

## 2. Arquitectura

```
data/consumos/*.csv ─┐
data/maestros/*.csv ─┼─► src/pipeline.py (rol_ingesta) ─► PostgreSQL ─► out/*.csv|json
data/facturacion/*.json┘   lectores → calidad (B3/B4) →       core / cuarentena / control / audit / analytics
                           cifrado + PII → upsert por lotes
data/eventos/*.ndjson ─► tools/producer.py ─► Redpanda ─► src/stream_consumidor.py ─► out/alertas.json
```

| Pieza | Archivo |
|---|---|
| Lectura con detección de encoding, separador, columnas, zona horaria y unidad | `src/lectores.py` |
| Reglas B3/B4 como funciones puras | `src/calidad.py` |
| Orquestación, transacción por archivo, salto por hash, bitácora | `src/pipeline.py` |
| Maestros (PII cifrada) y facturación (B6) | `src/maestros.py`, `src/facturacion.py` |
| Cifrado AES-256-GCM, HMAC del NIT, enmascarado de PII | `src/cifrado.py`, `src/pii.py` |
| Clasificación viva | `data_classification.yaml`, `src/clasificacion.py` |
| Salidas y logs JSON | `src/salidas.py`, `src/logs.py` |
| Alertas en streaming | `src/alertas.py`, `src/stream_consumidor.py` |
| Modelo, roles, bitácora | `sql/01..05_*.sql` |
| Infraestructura en AWS (descrita, no desplegada) | `infra/*.tf` |
| Exploración inicial de los datos | `scripts/exploracion.py` |
| Huella para comprobar idempotencia | `scripts/huella.sql` |

**Modelo** (estrella, ADR-003): `core.dim_cliente` (PII en `BYTEA` cifrado + `nit_hash`),
`core.dim_frontera`, `core.dim_limite_frontera`, `core.fact_consumo_horario` (PK
`(frontera_id, ts_medicion TIMESTAMPTZ)`, `kwh NUMERIC` con `CHECK`), `core.fact_factura`,
`core.fact_factura_componente`; cuarentena por tipo de rechazo, sin PII; `control.archivo_procesado`
(hash SHA-256 y conteos por archivo); `audit.bitacora_corridas`; `analytics.v_consumo_mensual`.

---

## 3. Reglas que implementa

En el código, las etiquetas **`[README B3]`**, **`[README C4]`**, etc. apuntan a esta sección.
Las otras etiquetas son `[Exploración]` (salió de analizar los datos reales) y `[Prevención]`
(evita una falla concreta que los datos todavía no traen).

### A · Ingesta e idempotencia
- Se procesa **lo que haya** en `data/consumos/` (orden lexicográfico), no una lista fija de meses.
  El formato (encoding, separador, nombre de columnas, zona horaria, unidad) se **detecta** por archivo.
- **Idempotencia:** dos `make run` seguidos dejan exactamente el mismo estado (huella SHA-256 por
  archivo + upsert por `(frontera_id, ts_medicion)`).
- **Datos tardíos:** un archivo de corrección reemplaza lecturas ya cargadas sin recargar todo.
- **Cuarentena:** lo rechazado no se borra; queda consultable con su motivo.
- **Robustez:** un archivo truncado o corrupto se rechaza entero; los meses ya cargados no cambian.

### B · Calidad de datos
| # | Regla |
|---|---|
| B1 | Mes y hora en hora local de Colombia (`America/Bogota`, UTC−5 fijo). Los archivos en UTC se normalizan. |
| B2 | El resultado siempre en **kWh**; MWh se convierte (× 1000, con `Decimal`). |
| B3 | Para una misma `(frontera_id, timestamp)` gana la mayor `version_lectura`; si empatan, la última en orden de archivo (nombre lexicográfico) y de fila. |
| B4 | Se descartan y reportan: valor nulo, negativo o `> 1.000.000 kWh`. Se aplica **antes** de B3 (ADR-004). |
| B5 | Una hora sin lectura válida es un hueco: se reporta, no se rellena. |
| B6 | Una factura cuya frontera no existe en el maestro va a cuarentena. |
| B7 | Entradas hostiles en el maestro (comillas anidadas, saltos de línea en un campo, cadenas enormes, texto con SQL) son datos, no código. Ninguna fila se pierde en silencio. |

Resultado sobre el conjunto de datos de referencia (`out/dq_report.json`):

| Llave | Valor |
|---|---:|
| `filas_leidas` | 115.340 |
| `duplicados_exactos` / `duplicados_conflictivos` | 180 / 120 |
| `registros_negativos` / `registros_nulos` / `registros_outlier` | 90 / 60 / 45 |
| `registros_mwh_convertidos` | 400 |
| `correcciones_tardias_aplicadas` | 300 |
| `horas_sin_lectura_valida_total` | 655 |
| `fronteras_huerfanas_facturacion` | 12 |
| `clientes_con_pii_en_texto_libre` | 25 |

### C · Seguridad, privacidad y gobernanza
Marco: Ley 1581 de 2012 (protección de datos personales) e ISO/IEC 27001:2022. Detalle y comandos
de verificación en [`SECURITY.md`](SECURITY.md).

| # | Control |
|---|---|
| C1 | `data_classification.yaml` etiqueta cada columna (`Público` / `Interno` / `Confidencial` / `Restringido-PII`); el pipeline lo lee para decidir qué cifrar y una prueba exige que esté completo. |
| C2 | Correos, celulares y cédulas escritos en texto libre (`observaciones`) se detectan y enmascaran. |
| C3 | PII cifrada con **AES-256-GCM en la aplicación** (la llave nunca llega a la base) + HMAC del NIT para cruzar facturas y clientes sin exponerlo. |
| C4 | Tres roles de menor privilegio: `rol_ingesta` (escribe, no lee PII en claro), `rol_analista` (solo la vista analítica), `rol_auditor` (solo la bitácora). El pipeline no usa el superusuario. |
| C5 | Bitácora **append-only**: `UPDATE`/`DELETE`/`TRUNCATE` revocados + triggers que los bloquean incluso al superusuario. |
| C6 | Cero credenciales en el código: todo en `.env`, fuera de git. |
| C7 | Dependencias auditadas con `pip-audit` (47 vulnerabilidades del prototipo inicial → 0) y fijadas con `==`. |
| C8 | [`THREAT_MODEL.md`](THREAT_MODEL.md): 12 amenazas STRIDE sobre esta arquitectura, con control y mapeo al Anexo A de ISO 27001. |

### D · Streaming
Consumidor del tópico `mediciones.raw` sobre una rejilla de 96 intervalos de 15 minutos:

| Alerta | Condición |
|---|---|
| `FRONTERA_SIN_REPORTE` | 3 intervalos consecutivos sin medición (una alerta por caída; `ts` del tercer intervalo). |
| `CONSUMO_FUERA_DE_BANDA` | `kwh` estrictamente mayor que el `max_kwh` de la frontera. |
| `LECTURA_RETROACTIVA` | Llega una medición anterior a la última ya procesada de esa frontera. |

### E · Observabilidad
Logs JSON (una línea por evento) con filtro anti-PII, `out/metrics.json`, bitácora por etapa y
[`RUNBOOK.md`](RUNBOOK.md) para responder a fallas.

### F · Infraestructura como código
`infra/`: S3 con SSE-KMS, versionado, bloqueo público y solo HTTPS; KMS con rotación y política;
roles IAM equivalentes a C4. `terraform validate` pasa. **No está desplegado.**

---

## 4. Endurecimientos para el despliegue en AWS

- **Kafka sin TLS ni SASL.** Escucha solo en `127.0.0.1`; riesgo aceptado en `THREAT_MODEL.md`.
- **Bitácora sin copia inmutable externa.** Un superusuario puede desactivar el trigger; en un
  despliegue real iría a S3 Object Lock o CloudTrail.
- **Llaves fuera de un KMS.** En local viven en `.env` (generadas al azar, fuera de git);
  `infra/kms.tf` describe la versión con AWS KMS.
- **`FRONTERA_SIN_REPORTE` se evalúa al terminar de consumir**, no con ventanas en tiempo real.
- **Terraform mínimo:** S3, KMS e IAM; sin VPC, RDS ni CloudTrail.

Las decisiones de diseño y los trade-offs están en [`DECISIONS.md`](DECISIONS.md) (ADRs, tabla de
ambigüedades y hallazgos de la exploración de datos).

---

**Autor:** Anderson Sarmiento · Desarrollado con apoyo de Claude Code; cada decisión de diseño fue
revisada y verificada contra los datos.
