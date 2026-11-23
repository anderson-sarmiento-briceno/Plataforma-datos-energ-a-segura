# Seguridad de la solución

Este documento resume los controles de seguridad (C1–C8 del `README.md`) y cómo verificarlos. El detalle de cada
decisión está en `DECISIONS.md`, y las amenazas en `THREAT_MODEL.md`.

---

## 1. Clasificación de datos

`data_classification.yaml` etiqueta **todas** las columnas de todas las tablas y de la vista en
cuatro niveles: `Público`, `Interno`, `Confidencial`, `Restringido-PII`.

Cómo lo consume el pipeline (`src/clasificacion.py`):
- Las columnas `Restringido-PII` declaran su **origen**, por ejemplo
  `nit_cifrado: {nivel: Restringido-PII, origen: nit}`. El pipeline lee el YAML para decidir **qué
  cifrar y desde qué columna** (`columnas_a_cifrar`). Si el YAML pidiera cifrar una columna que la
  tabla no tiene, la carga falla en vez de guardar el dato en claro.
- `tests/test_clasificacion.py` compara el YAML con `information_schema.columns`: **una columna
  nueva sin clasificar hace fallar `make test`**. Otra prueba verifica que la vista del analista no
  tenga ninguna columna `Restringido-PII`.

| Nivel | Ejemplos | Tratamiento |
|---|---|---|
| Restringido-PII | NIT, correo, celular, dirección, representante legal, observaciones originales, correo de facturación | Cifrado AES-256-GCM; nunca en vistas, logs ni `out/` |
| Confidencial | razón social, consumos, valores de factura, `nit_hash`, observaciones enmascaradas | En claro en `core`; fuera de la vista del analista (salvo el consumo agregado) |
| Interno | IDs, fechas, ciudad, conteos, cuarentena de consumos | En claro |
| Público | operador de red, concepto de factura (G, T, D, C, P, R) | En claro |

## 2. Cifrado

| Qué | Dónde | Mecanismo | Gestión de la llave |
|---|---|---|---|
| NIT, correo, celular, dirección, representante legal y observaciones originales del cliente | `core.dim_cliente.*_cifrado` / `*_cifrada` (`BYTEA`) | AES-256-GCM en la aplicación (`src/cifrado.py`, librería `cryptography`); nonce aleatorio de 12 bytes por valor, guardado al inicio del dato | `LLAVE_CIFRADO` (32 bytes) solo en `.env`; la genera `make setup` con `secrets` si no existe. No está en git ni en `.env.example`. **Nunca viaja a Postgres** |
| Correo de facturación | `core.fact_factura.email_facturacion_cifrado` (`BYTEA`) | Igual | Igual |
| NIT para cruces | `core.dim_cliente.nit_hash`, `core.fact_factura.nit_hash` | HMAC-SHA256 del NIT normalizado (solo dígitos) | `LLAVE_HMAC`, **distinta** de la de cifrado (separación de llaves), también solo en `.env` |

**Joins sobre campos sensibles sin exponerlos:** facturas y clientes se cruzan por `nit_hash`. El
NIT llega en 4 formatos (`999.999.999-9`, `999999999-9`, `NIT 999999999-9`, `9999999999`); se
normaliza a dígitos antes del HMAC, así que el mismo NIT da la misma huella (160 de 160 facturas
cruzan con su cliente). Se usa HMAC y no un hash simple porque un NIT tiene solo 10 dígitos: sin
llave, todas las huellas posibles se podrían precalcular.

**Por qué en la aplicación y no con pgcrypto:** con pgcrypto la llave tendría que enviarse a la
base en cada consulta (y podría quedar en logs o en `pg_stat_statements`). Así, quien obtenga un
dump o acceso de DBA solo ve bytes. En producción, la llave vendría de AWS KMS con envelope
encryption (`infra/`).

**Riesgo aceptado:** si se pierde `LLAVE_CIFRADO`, la PII cifrada no se recupera. En producción el
KMS da respaldo y rotación.

## 3. Control de accesos (menor privilegio)

| Rol | Puede | NO puede | Dónde está el GRANT |
|---|---|---|---|
| `rol_ingesta` | `SELECT/INSERT/UPDATE` en `core`; `SELECT/INSERT` en `cuarentena`; `SELECT/INSERT/UPDATE` en `control`; `DELETE` solo en `core.fact_consumo_horario` y `cuarentena.consumo_rechazado` (recarga de un archivo que cambió); **solo `INSERT`** en la bitácora | Borrar o vaciar tablas (`DROP`, `TRUNCATE`); leer, cambiar o borrar la bitácora; leer la vista; leer PII en claro (solo ve bytes cifrados, no tiene la llave) | `sql/04_roles_permisos.sql` |
| `rol_analista` | `SELECT` en `analytics.v_consumo_mensual` (`frontera_id, mes, consumo_kwh, horas_con_dato`) | Ver `core`, `cuarentena`, `control` o `audit`; escribir; crear tablas | `sql/04_roles_permisos.sql` |
| `rol_auditor` | `SELECT` en `audit.bitacora_corridas` | Todo lo demás, incluido modificar la bitácora | `sql/04_roles_permisos.sql` |
| `PUBLIC` | Nada | `CONNECT` a la base; usar cualquier esquema, incluido `public` | `sql/04_roles_permisos.sql` (`REVOKE ... FROM PUBLIC`) |
| `energia_admin` (superusuario) | Dueño de todas las tablas | — (es la cuenta de administración; el pipeline **no** la usa) | Imagen de Postgres + `.env` |

- El pipeline se conecta como **`rol_ingesta`** (`src/bd.py`, `conectar("ingesta")`).
- Las contraseñas de los roles no están en ningún `.sql`: `make up` las pasa al contenedor con
  `docker compose exec -e VARIABLE` (sin el valor en la línea de comandos) y `psql` las lee con
  `\getenv`.
- Ningún rol de trabajo es dueño de las tablas, así que ninguno puede alterarlas ni borrarlas.

## 4. Enmascaramiento de PII

- **Columnas de PII estructuradas** (NIT, correo, celular, dirección, representante legal): no se
  enmascaran, se **cifran** (sección 2). No existen en claro en la base.
- **PII en texto libre** (`clientes.csv` → `observaciones`), en `src/pii.py`, con expresiones
  regulares:

| Dato | Patrón | Máscara |
|---|---|---|
| Correo | `usuario@dominio.tld` | `u***@dominio.tld` (se conserva el dominio: indica la empresa, no la persona) |
| Celular | `3` + 9 dígitos, sin dígito ni letra delante y sin dígito detrás | `3*******67` |
| Cédula | `CC` + 6 o más dígitos | `CC ******78` |

- Se guarda `observaciones_enmascarada` (texto) y `observaciones_cifrada` (el original, cifrado),
  así no se pierde información.
- **Clientes afectados: 25** (14 correos, 11 celulares, 6 cédulas). El número va en
  `dq_report.json` como `clientes_con_pii_en_texto_libre`.
- El patrón de celular exige que no haya una **letra** delante, para no confundirlo con los
  códigos de frontera (`CO3001957318`). Sin esa regla, un volcado de la base daba 14.409 falsos
  positivos.
- Las mismas expresiones son la red de seguridad de los logs (Paso 8).

## 5. Gestión de secretos

**Cómo se inyectan:** todas las credenciales están en `.env` (en `.gitignore`), cargado con
`python-dotenv` (`src/config.py`) y por `docker compose`. `make setup` crea `.env` desde
`.env.example` y **genera al azar** las dos llaves de cifrado. Si falta una variable, el pipeline y
Compose fallan con un mensaje claro (`${VAR:?…}`, `cargar_llave`).

**Credenciales de desarrollo:** las contraseñas de `.env.example` son **solo para desarrollo
local** (permiten clonar y correr sin configurar nada). Postgres solo
escucha en `127.0.0.1`. En producción vendrían de un gestor de secretos (AWS Secrets Manager). Ver
ADR-002.

**Lo que encontré en el prototipo inicial y qué hice:**
| Hallazgo | Acción |
|---|---|
| `config/pipeline.yaml`: contraseña de Postgres en claro | Eliminada **antes del primer commit**: nunca entró a git. Se considera **comprometida**: no se reutiliza en ningún lado, y en un entorno real se rotaría |
| `config/pipeline.yaml`: credenciales de MinIO por defecto | Eliminadas; MinIO se quitó (ADR-001) |
| `config/pipeline.yaml`: "llave maestra" en base64 con `algoritmo: none` | Eliminada. Base64 no es cifrado; se reemplazó por AES-256-GCM con llave aleatoria |
| `config/pipeline.yaml`: webhook de Slack | Eliminado. Un webhook es una credencial (quien lo tenga puede publicar en el canal) |
| `docker-compose.yml`: `postgres/postgres`, `minioadmin`, puertos en `0.0.0.0` | Credenciales desde `.env`, superusuario no por defecto (`energia_admin`), puertos solo en `127.0.0.1` |

**Verificación realizada:**
- `git log -p --all` (todo el historial): la contraseña del prototipo, la llave falsa y el webhook
  aparecen **0 veces** (buscados leyendo los valores del archivo original, sin escribirlos).
- `git grep` de `password|secret|token|api_key|hooks.slack|minioadmin` en `src/`, `config/`,
  `sql/`, `tests/`, `scripts/`, `Makefile` y `docker-compose.yml`: solo **nombres de variables**,
  ningún valor.
- Ningún `.env`, `.pem` ni `.key` versionado.

## 6. Trazabilidad y auditoría

**Esquema** (`audit.bitacora_corridas`, `sql/02_tablas.sql`):

| Columna | Contenido |
|---|---|
| `id`, `corrida_id` | Identificador de la fila y de la corrida (UUID, agrupa las etapas de una corrida) |
| `usuario_bd` | Quién: `current_user` (lo pone Postgres; el pipeline corre como `rol_ingesta`) |
| `etapa` | Qué: `maestros`, `consumos`, `facturacion` y `corrida` (resumen) |
| `inicio`, `fin` | Cuándo |
| `filas_leidas`, `filas_cargadas`, `filas_rechazadas` | Cuántas |
| `resultado`, `tipo_error` | `OK` / `ERROR`, y **solo el tipo** de error (nunca el mensaje, que puede traer valores) |

**Append-only de verdad** (`sql/05_auditoria.sql`), en dos capas:
1. **Permisos:** `REVOKE UPDATE, DELETE, TRUNCATE` a `PUBLIC` y a los tres roles. `rol_ingesta`
   solo tiene `INSERT`; `rol_auditor`, solo `SELECT`.
2. **Triggers:** `BEFORE UPDATE OR DELETE` (por fila) y `BEFORE TRUNCATE` (por sentencia) llaman a
   `audit.bloquear_cambios()`, que lanza un error. Bloquean el cambio **aunque quien lo intente
   tenga el permiso**, incluido el superusuario (probado).

**Límite conocido:** un superusuario puede desactivar el trigger. En producción la bitácora se
copiaría además a un almacenamiento inmutable (S3 con Object Lock o CloudTrail).

## 7. Dependencias

Herramienta: **`pip-audit` 2.10.1**, instalado en un entorno virtual aparte (no es dependencia del
proyecto), contra la base de datos de vulnerabilidades de PyPI. Auditado el 2026-11-12.

| | Resultado |
|---|---|
| `requirements.txt` **del prototipo** | **47 vulnerabilidades conocidas en 4 paquetes**: `urllib3 1.24.1` (25), `requests 2.19.1` (10), `Jinja2 2.11.2` (10), `PyYAML 5.3.1` (2, entre ellas PYSEC-2021-142: ejecución de código al cargar YAML con `full_load`). Además, no instalaba en Python 3.14 |
| `requirements.txt` **actual** | **0 vulnerabilidades conocidas** (`pip-audit -r requirements.txt`, con dependencias transitivas) |

**Qué se cambió:**
- **Se quitaron** `requests`, `urllib3` y `Jinja2`: el proyecto no los usa. Un paquete sin uso es
  superficie de ataque gratis.
- **Se actualizaron y fijaron** con `==`: `pandas 2.3.3`, `psycopg2-binary 2.9.13`,
  `PyYAML 6.0.3`; **se agregaron** `python-dotenv 1.2.4`, `kafka-python 2.3.3`,
  `cryptography 50.0.2` y `pytest 9.1.1`. Todas con más de dos semanas publicadas.
- **YAML siempre con `yaml.safe_load`** (`src/config.py`, `src/clasificacion.py`), que no puede
  ejecutar código.
- **Imágenes Docker con versión fija** (`postgres:16.15-alpine`, `redpandadata/redpanda:v26.2.6`)
  en lugar de `:latest`.

Para repetir la auditoría:
```bash
python3 -m venv /tmp/auditoria && /tmp/auditoria/bin/pip install pip-audit
/tmp/auditoria/bin/pip-audit -r requirements.txt
```

## 8. Cómo verificar todo lo anterior

Con la infraestructura arriba y los datos cargados:
```bash
make setup && make up && make run
```

**Pruebas automáticas** (incluyen permisos por rol, bitácora, cifrado, PII y clasificación):
```bash
make test        # 72 pruebas; sin base de datos, las que la necesitan se saltan
```

**C3 · Cifrado en reposo, como superusuario** (la PII debe verse como bytes `\x…`):
```bash
docker compose exec postgres psql -U energia_admin -d energia_datos -c \
  "SELECT cliente_id, nit_hash, nit_cifrado, email_cifrado, observaciones_enmascarada FROM core.dim_cliente LIMIT 3;"
```

**C4 · Menor privilegio** (cada comando pide la contraseña de `.env`; los marcados ✗ deben fallar con `permission denied`):
```bash
psql -h 127.0.0.1 -U rol_analista -d energia_datos -c "SELECT * FROM analytics.v_consumo_mensual LIMIT 3;"  # ✓
psql -h 127.0.0.1 -U rol_analista -d energia_datos -c "SELECT * FROM core.dim_cliente LIMIT 1;"             # ✗
psql -h 127.0.0.1 -U rol_auditor  -d energia_datos -c "SELECT * FROM audit.bitacora_corridas;"              # ✓
psql -h 127.0.0.1 -U rol_auditor  -d energia_datos -c "DELETE FROM audit.bitacora_corridas;"                # ✗
psql -h 127.0.0.1 -U rol_ingesta  -d energia_datos -c "DROP TABLE core.fact_consumo_horario;"               # ✗
psql -h 127.0.0.1 -U rol_ingesta  -d energia_datos -c "UPDATE audit.bitacora_corridas SET resultado='X';"   # ✗
```
(Sin `psql` en el equipo, se puede usar el del contenedor:
`docker compose exec postgres psql -h 127.0.0.1 -U rol_analista -d energia_datos -c "…"`.)

**C5 · La bitácora no se puede modificar ni siquiera como superusuario:**
```bash
docker compose exec postgres psql -U energia_admin -d energia_datos -c "DELETE FROM audit.bitacora_corridas;"
# ERROR:  audit.bitacora_corridas es append-only: DELETE no está permitido
```

**C6 · Secretos fuera del código y del historial:**
```bash
git grep -n -i -E "password|secret|token|hooks\.slack" -- src config sql tests   # solo nombres de variables
git ls-files | grep -E "\.env$"                                                    # vacío
```

**C7 · Dependencias:** ver la sección 7.
