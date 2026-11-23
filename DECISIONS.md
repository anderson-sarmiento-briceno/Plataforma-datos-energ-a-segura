# Decisiones de arquitectura (ADRs)

> Formato corto. Lo importante es el **trade-off**: qué se descartó y por qué.
> Al final, los hallazgos de la exploración de datos que motivaron varias decisiones.

---

## ADR-001 — Quitar MinIO de la infraestructura local

**Estado:** Aceptada
**Fecha:** 2026-11-11

**Contexto**
El `docker-compose.yml` del prototipo inicial levantaba MinIO (almacenamiento tipo S3) con credenciales por
defecto y puertos abiertos en `0.0.0.0`. El pipeline lee los archivos directamente de `data/`,
que es donde llegan.

**Opciones consideradas**
1. Mantener MinIO endurecido y cargar los archivos al bucket antes de leerlos.
2. Quitar MinIO y leer de `data/`.

**Decisión**
Opción 2.

**Por qué**
Un servicio que no se usa solo agrega superficie de ataque, una descarga más y otro punto de
falla en `make up`. Lo que se resigna es parecido con producción, donde los crudos estarían en
S3. Eso se cubre en `infra/` (Terraform), con un bucket S3 cifrado con KMS.

**Consecuencias**
`make up` es más rápido y simple. Si en el futuro los archivos llegan por S3, el cambio queda
aislado en el módulo que lee archivos; el resto del pipeline no sabe de dónde vienen.

---

## ADR-002 — Credenciales de desarrollo local en `.env.example`

**Estado:** Aceptada
**Fecha:** 2026-11-11

**Contexto**
La regla es no versionar secretos. Pero quien clona el repo debe poder correr
`make setup && make up && make run` sin configurar nada a mano, y las herramientas y los
comandos de `SECURITY.md` necesitan conocer los usuarios de cada rol.

**Opciones consideradas**
1. Credenciales fijas, solo de desarrollo local, en `.env.example`.
2. Que `make setup` genere contraseñas aleatorias en `.env`.

**Decisión**
Opción 1 para las contraseñas de la base; las **llaves de cifrado sí se generan al azar** en
`make setup` (son las que protegen la PII).

**Por qué**
La opción 2 es más elegante, pero obliga a leer `.env` antes de poder conectarse con cada rol para
revisar los permisos. Las credenciales elegidas no tienen relación con el secreto del prototipo
inicial (que se considera comprometido), y Postgres solo escucha en `127.0.0.1`.

**Consecuencias**
Cualquiera con el repo conoce estas contraseñas: solo sirven para la base local. En
producción vendrían de un gestor de secretos (ver `SECURITY.md`).

---

## ADR-003 — Modelo dimensional con la hora como instante (`TIMESTAMPTZ`)

**Estado:** Aceptada
**Fecha:** 2026-11-11

**Contexto**
Hay que guardar consumos horarios que llegan unos en hora local y otros en UTC, cargar
correcciones sin recargar todo, apartar lo rechazado y dar al analista una vista sin PII.

**Opciones consideradas**
1. Tabla ancha única con todo (clientes, fronteras y consumos juntos).
2. Modelo en estrella: dimensiones (`dim_cliente`, `dim_frontera`, `dim_limite_frontera`) y
   hechos (`fact_consumo_horario`, `fact_factura`, `fact_factura_componente`), con esquemas
   separados para `cuarentena`, `control`, `audit` y `analytics`.
3. Igual que la 2, más un esquema `staging` para cargar crudo y deduplicar en SQL.

**Decisión**
Opción 2. La hora se guarda como `ts_medicion TIMESTAMPTZ` y el mes se calcula en la vista con
`AT TIME ZONE 'America/Bogota'`.

**Por qué**
- La estrella separa la PII (solo en `dim_cliente`) de los hechos. Los permisos y el cifrado se
  aplican en un solo lugar, y la vista del analista no necesita tocar la dimensión de clientes.
- La PK `(frontera_id, ts_medicion)` es exactamente la llave de la regla B3: el upsert decide qué
  lectura gana sin recargar el mes.
- `TIMESTAMPTZ` guarda un **instante**: `2026-02-01T04:30Z` y `2026-01-31T23:30-05:00` son la misma
  fila (verificado: la PK rechaza el segundo). Así no hay duplicados disfrazados por zona horaria.
- Se descartó `staging` (opción 3): B3 se resuelve en pandas antes de cargar, y un esquema sin uso
  sería una zona más que proteger y explicar.
- Lo que se resigna: el mes no está guardado, se calcula en cada consulta. Con ~115.000 filas el
  costo es despreciable.

**Consecuencias**
- `kwh` es `NUMERIC` sin escala (exacto, sin redondeos silenciosos) con un `CHECK` 0–1.000.000
  como red de seguridad de B4.
- La cuarentena tiene PK por `(archivo, fila)`, así que re-correr no duplica rechazos.
- Cambiar una columna exige `make clean` (el DDL usa `IF NOT EXISTS` y no altera tablas que ya
  existen). Para producción haría falta una herramienta de migraciones.

---

## ADR-004 — Inválidos antes que duplicados, y correcciones resueltas antes de cargar

**Estado:** Aceptada
**Fecha:** 2026-11-11

**Contexto**
La regla B3 define (duplicados: gana la versión mayor, luego el archivo y la fila posteriores)
y B4 (inválidos: se descartan), pero no dice en qué orden aplicarlas. Si en una misma hora la
lectura "ganadora" es inválida, el orden cambia el resultado: hueco o lectura válida anterior.
Además, la corrección de abril llega en otro archivo y debe reemplazar lo ya cargado sin recargar
el mes, y el pipeline debe poder contar `correcciones_tardias_aplicadas`.

**Opciones consideradas**
1. B3 primero: elegir la ganadora entre todas las lecturas y, si es inválida, dejar la hora como hueco.
2. B4 primero: descartar las inválidas y elegir la ganadora solo entre las válidas.
3. Para las correcciones: confiar solo en `ON CONFLICT ... DO UPDATE` en la base.
4. Para las correcciones: leer las llaves ya cargadas del rango, decidir en pandas quién gana
   (y contarlo), y dejar el `ON CONFLICT ... WHERE` como segunda red.

**Decisión**
Opción 2 para el orden, y opción 4 para las correcciones.

**Por qué**
- Con B4 primero, una corrección que llega inválida (por ejemplo, negativa) no borra una lectura
  válida: va a cuarentena y la hora conserva su dato. Es además el comportamiento natural de una
  carga por archivo: lo inválido nunca llega a la base, así que no puede "ganar".
- En estos datos el orden no cambia ningún número (ningún grupo duplicado mezcla válidos con
  inválidos; verificado con `scripts/exploracion.py`); solo importa para un mes nuevo.
- Decidir las correcciones en pandas permite **contarlas** de forma explícita (filas que reemplazan
  una lectura de otro archivo). Con solo `ON CONFLICT` la base no dice cuántas filas reemplazó.
- El desempate por nombre de archivo se compara con `COLLATE "C"` (orden de bytes, igual que
  Python). Con una intercalación de idioma (ICU), `…04_correccion.csv` quedaría antes que `…04.csv`
  porque ignora la puntuación: verificado en Postgres.
- Lo que se resigna: una consulta más por archivo (leer las llaves del rango) y que la regla de
  desempate exista en dos lugares (Python y SQL). Se acepta porque el SQL es solo una red de
  seguridad y ambos se prueban.

**Consecuencias**
- Las lecturas descartadas por B3 (`DUPLICADO_EXACTO`, `DUPLICADO_CONFLICTIVO`) y las que pierden
  contra una ya cargada (`SUPERADA_POR_VERSION_MAYOR`) también quedan en cuarentena.
- `correcciones_tardias_aplicadas` = 300 en estos datos.

---

## ADR-005 — Las salidas se generan desde la base, y el CSV con el rol analista

**Estado:** Aceptada
**Fecha:** 2026-11-12

**Contexto**
`make run` debe dejar `out/consumo_mensual.csv`, `out/dq_report.json` y `out/metrics.json`. Se pueden
borrar en cualquier momento, `make run` debe dar lo mismo **dos veces** (la segunda no procesa nada
porque todo se salta por hash) y `out/` nunca puede contener PII.

**Opciones consideradas**
1. Escribir los archivos con lo que procesó **esta** corrida (en memoria).
2. Escribirlos desde lo **guardado** en la base: la vista analítica para el CSV, y los conteos de
   `control.archivo_procesado` y de la cuarentena para el reporte.
3. Para el CSV: consultarlo con el rol de ingesta o con el rol analista.

**Decisión**
Opción 2, y el CSV se consulta con **`rol_analista`**.

**Por qué**
- Con la opción 1, la segunda corrida (que salta todo) reportaría ceros. Desde la base, los dos
  `make run` dan archivos idénticos (verificado con `diff`).
- El CSV y la vista del analista salen de la **misma consulta**: no hay dos cálculos que puedan
  diferir.
- Con `rol_analista`, el CSV **no puede** contener PII aunque alguien cambie la consulta: Postgres
  la rechazaría por permisos. Es una garantía de la base, no solo de la disciplina del código.
- Lo que se resigna: una conexión más a la base por corrida (despreciable).

**Consecuencias**
- `metrics.json` tiene dos bloques: `corrida` (lo que hizo esta corrida) y `acumulado` (estado de
  la base).
- Los conteos de calidad deben guardarse al cargar cada archivo (ya se hacía en `control`).

---

## Decisiones sobre las ambigüedades de las reglas

| # | Ambigüedad | Decisión | ¿Cambia los números en estos datos? |
|---|---|---|---|
| 1 | Si en una hora duplicada la lectura ganadora es inválida | B4 antes que B3: lo inválido no compite (ADR-004) | No: ningún grupo duplicado mezcla válidos e inválidos |
| 2 | `fronteras_huerfanas_facturacion`: ¿fronteras o facturas? | Fronteras **distintas** (la llave dice "fronteras") | No: 12 facturas de 12 fronteras |
| 3 | Huecos de una frontera sin ninguna fila en un mes | Cuentan **todas** las horas de ese mes (B5: "da igual si la fila no existía"). Se recorren todas las fronteras del maestro en todos los meses con datos | No: las 40 fronteras reportan los 4 meses. Total 655 |
| 4 | `correcciones_tardias_aplicadas`: ¿todas las de versión mayor o solo las que reemplazan? | Las que **reemplazan una lectura de otro archivo** | No: las 300 reemplazan una lectura válida de abril |
| 5 | Archivo corrupto con filas buenas | Si la **estructura** está rota (filas cortadas o con campos de más, columnas faltantes, no parseable) se rechaza **entero**; si solo hay **valores** malos, esas filas van a cuarentena | — |
| 6 | `filas_leidas`: ¿incluye el archivo de corrección? | Sí: son filas leídas (115.340) | — |
| 7 | `registros_mwh_convertidos`: ¿convertidas o cargadas? | Filas válidas que venían en MWh | No: las 400 se cargan |

---

## Hallazgos de la exploración de datos

Obtenidos con `scripts/exploracion.py`, que solo imprime conteos (nunca valores de PII).

### Consumos
| Archivo | Separador | Hora | Valor | Filas | Vacíos | Negativos | Outliers | Sobrantes (exactos / conflictivos) |
|---|---|---|---|---:|---:|---:|---:|---|
| `consumos_2026-01.csv` | `,` | `timestamp` local | `consumo_kwh` | 29.685 | 15 | 18 | 17 | 79 (48 / 31) |
| `consumos_2026-02.csv` | `;`, coma decimal | `timestamp_utc` (UTC) | `consumo_kwh` | 26.855 | 10 | 22 | 8 | 63 (40 / 23) |
| `consumos_2026-03.csv` | `,` | `timestamp` local | `kwh` + `unidad` (400 en MWh) | 29.718 | 20 | 26 | 12 | 83 (51 / 32) |
| `consumos_2026-04.csv` | `,` | `timestamp` local | `consumo_kwh` | 28.782 | 15 | 24 | 8 | 75 (41 / 34) |
| `consumos_2026-04_correccion.csv` | `,` | `timestamp` local | `consumo_kwh` | 300 | 0 | 0 | 0 | 0 |

- Total de duplicados sobrantes: **300 (180 exactos + 120 conflictivos)**.
- Todos los archivos usan CRLF y UTF-8. Ningún valor es no numérico (aparte de los vacíos).
- Febrero va de `2026-02-01T05:00Z` a `2026-03-01T04:00Z`: en hora de Bogotá es febrero completo.
- Los outliers llegan a ~1,2 × 10¹⁰ kWh.
- Todas las fronteras de consumos existen en el maestro.

### Columna `calidad_lectura` (ninguna regla la usa)
- Solo trae `OK` (versión 1) y `CORREGIDA` (versión 2, archivo de corrección).
- **Decisión:** no se usa como regla de validación: es redundante con `version_lectura`. Se
  guarda en la tabla de hechos por trazabilidad. Si un mes nuevo trae otro valor, se reporta en
  los logs pero no se rechaza la fila (no hay reglas definidas para esta columna).

### Correcciones tardías
- Las 300 filas del archivo de corrección pisan una llave que **ya existía** en abril.
- Ninguna pisa una lectura inválida ni una llave duplicada.
- Por eso, en estos datos, `correcciones_tardias_aplicadas` vale 300 sin importar cómo se
  interprete (ambigüedad: "solo las que reemplazan" vs "todas las de versión mayor").

### Duplicados vs inválidos
- Ningún grupo duplicado mezcla un valor válido con uno inválido. El orden "primero B3 o
  primero B4" no cambia los números en estos datos, pero sí podría en un mes nuevo.

### Maestros
- `clientes.csv`: Latin-1, 42 líneas físicas → 40 clientes con un parser CSV real.
  - NIT en 4 formatos: `999999999-9` (12), `999.999.999-9` (10), `NIT 999999999-9` (9),
    `9999999999` (9).
  - Entradas hostiles: comillas y saltos de línea dentro de campos (CLI0013, CLI0022), razón
    social de 1.632 caracteres, y un texto con forma de inyección SQL.
  - **25 clientes** con PII en `observaciones` (14 con correo, 11 con celular, 6 con cédula; un
    cliente puede tener varias).
- `fronteras.csv`: 40 fronteras, todas `ACTIVA`, registradas entre 2024 y 2025. `estado` y
  `fecha_registro` no afectan ningún cálculo en estos datos.
- `limites_frontera.csv`: 40 límites, todos coherentes (`min < max`).

### Facturación
- 4 archivos × 43 facturas. **3 huérfanas por mes (12 en total)**, todas sin componentes.
- Componentes siempre `G, T, D, C, P, R`.

### Eventos del stream
- 3.795 eventos con `evento_id` único, 40 fronteras, intervalos de 15 minutos.
- Simulación de las reglas: **22** fuera de banda, **5** retroactivas, **6** caídas de 3
  intervalos.
- `medidor` trae `serial` y `firmware`: datos del equipo, no PII.
