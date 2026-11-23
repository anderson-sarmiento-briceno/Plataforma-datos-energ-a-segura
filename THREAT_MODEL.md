# Modelo de amenazas — Plataforma de datos de consumo energético

Amenazas sobre **esta** arquitectura (pipeline batch en Python + PostgreSQL + Redpanda, corriendo
local con Docker), analizadas con STRIDE y mapeadas al Anexo A de ISO/IEC 27001:2022. Los controles
están descritos en `SECURITY.md` y las decisiones en `DECISIONS.md`.

## 1. Alcance y diagrama de flujo de datos

```
        FRONTERA DE CONFIANZA 1: archivos de terceros (no confiables)
 ┌──────────────────────────────────────────────────────────────────────────┐
 │ data/consumos/*.csv   data/maestros/*.csv (PII)   data/facturacion/*.json │
 │ data/eventos/*.ndjson → tools/producer.py → Redpanda (tópico mediciones.raw)│
 └───────────┬───────────────────────────────────────────────┬──────────────┘
             │ lectura                                        │ consumo (kafka-python)
             ▼                                                ▼
 ┌────────────────────────────── host (WSL / servidor) ─────────────────────────────┐
 │  src/pipeline.py  (rol_ingesta)            src/stream_consumidor.py                │
 │   lectores → calidad → cifrado/pii ──┐      alertas.py (reglas puras)              │
 │   .env: contraseñas, LLAVE_CIFRADO,  │                                              │
 │         LLAVE_HMAC (nunca a la base) │                                              │
 │  salidas: out/*.csv|json, logs/*.log │  out/alertas.json                            │
 └──────────────────────────────────────┼───────────────────────────────────────────┘
        FRONTERA DE CONFIANZA 2: red    │ TCP 127.0.0.1:5432 (scram-sha-256)
                                        ▼
 ┌──────────────────────────── PostgreSQL 16 (contenedor) ───────────────────────────┐
 │ core (dim_cliente con PII CIFRADA + nit_hash, hechos)   cuarentena (sin PII)        │
 │ control (hash por archivo)   audit.bitacora_corridas (append-only)                  │
 │ analytics.v_consumo_mensual (sin PII)                                               │
 └──────────────┬───────────────────────────────┬────────────────────────────────────┘
                │ FRONTERA 3: usuarios humanos   │
          rol_analista (solo la vista)     rol_auditor (solo la bitácora)
          superusuario energia_admin (DBA; con él se verifica C3)
```

**Fronteras de confianza:**
1. **Entrada de datos:** todo archivo o mensaje se trata como **no confiable** (puede venir truncado,
   corrupto u hostil: CLI0036 trae una inyección SQL).
2. **Aplicación ↔ base:** la base nunca recibe la llave de cifrado; el pipeline entra como
   `rol_ingesta`, no como superusuario.
3. **Personas ↔ base:** cada persona usa un rol con el mínimo permiso (analista, auditor).

**Fuera de alcance:** el sistema operativo del host, Docker Desktop y los equipos de quienes usan las salidas.

## 2. Activos y clasificación

| Activo | Dónde vive | Clasificación | Impacto si se compromete |
|---|---|---|---|
| PII de clientes (NIT, correo, celular, dirección, representante legal) | `clientes.csv` (entrada); `core.dim_cliente` **cifrada** | Restringido-PII | Alto: violación de la Ley 1581, sanción de la SIC, daño reputacional |
| PII en texto libre (`observaciones`) | `clientes.csv`; `dim_cliente`: enmascarada + original cifrada | Restringido-PII | Alto (igual) |
| Correo de facturación | `facturacion_*.json`; `core.fact_factura` cifrado | Restringido-PII | Alto |
| Llaves `LLAVE_CIFRADO` y `LLAVE_HMAC` | `.env` del host (no en git ni en la base) | Secreto | Crítico: quien las tenga con un dump lee toda la PII |
| Contraseñas de la base | `.env` (solo desarrollo local) | Secreto | Alto en producción; bajo aquí (solo `127.0.0.1`) |
| Consumos horarios y facturas | `core.fact_*` | Confidencial | Medio: base de la facturación; un error cambia lo que se cobra |
| Bitácora de corridas | `audit.bitacora_corridas` | Interno | Medio: pérdida de trazabilidad (no repudio) |
| Salidas (`out/`, `logs/`) | Disco del host | Interno / Confidencial | Alto si contuvieran PII |

## 3. Amenazas (STRIDE)

| # | Componente | STRIDE | Amenaza concreta | Prob. | Impacto | Control implementado | ¿Implementado? | ISO 27001:2022 Anexo A |
|---|---|---|---|---|---|---|---|---|
| T1 | Carga de `clientes.csv` → `core.dim_cliente` | **Tampering** / Elevation | Un texto del maestro (CLI0036: `'); DROP TABLE core.fact_consumo_horario;--`) se ejecuta como SQL y borra o vacía la tabla de consumos | A (ya viene en los datos) | A | **Todo** SQL con parámetros (`%s`, `execute_values`); el pipeline corre como `rol_ingesta`, que **no** puede hacer `DROP` ni `TRUNCATE` (no es dueño de las tablas). Probado en `tests/test_hostiles.py` y `tests/test_permisos.py` | Sí | A.8.28 Codificación segura · A.8.2 Derechos de acceso privilegiado |
| T2 | `core.dim_cliente` / volcado de la base | **Information disclosure** | Un DBA, un backup o un dump robado expone la PII de los 40 clientes | M | A | PII cifrada con **AES-256-GCM en la aplicación**; la llave nunca llega a Postgres. NIT solo como HMAC. Verificado volcando la base como superusuario: 0 valores reales en claro | Sí | A.8.24 Uso de criptografía · A.5.34 Privacidad y protección de PII |
| T3 | Rol `analista` | **Information disclosure** / Elevation | El analista consulta `core.dim_cliente` o la cuarentena y ve PII | M | A | `rol_analista` solo tiene `USAGE` en `analytics` y `SELECT` en la vista (sin columnas de PII); `PUBLIC` sin `CONNECT`. El CSV de salida se genera con este mismo rol | Sí | A.5.15 Control de acceso · A.8.3 Restricción del acceso a la información |
| T4 | `logs/pipeline.log`, `out/` | **Information disclosure** | Un log de error (p. ej. el `DETAIL` de psycopg2) o un log descuidado escribe un correo, celular o cédula | M | A | Solo se registran conteos, nombres de archivo y el **tipo** de error; cada línea pasa por el enmascarado de `src/pii.py` (red de seguridad). Verificado: 0 de 247 valores reales en `out/` y `logs/` | Sí | A.8.12 Prevención de fuga de datos · A.8.11 Enmascaramiento de datos · A.8.15 Registros |
| T5 | `audit.bitacora_corridas` | **Repudiation** / Tampering | Alguien (incluso con permisos amplios) borra o edita filas para ocultar una corrida o un error | B | M | `REVOKE UPDATE, DELETE, TRUNCATE` a todos + triggers que bloquean esos cambios **incluso al superusuario** (probado). Cada fila registra `current_user` | Parcial: un superusuario puede desactivar el trigger | A.8.15 Registros · A.5.33 Protección de los registros |
| T6 | Repositorio git / configuración | **Information disclosure** / Spoofing | La contraseña del prototipo inicial (`config/pipeline.yaml`) o una llave queda en el código o en el historial, y alguien se conecta a la base suplantando al pipeline | A (venía en el prototipo) | A | Secreto eliminado **antes del primer commit** (0 apariciones en `git log -p --all`) y considerado comprometido; credenciales solo en `.env`; llaves generadas al azar por `make setup`, nunca en git | Sí | A.5.17 Información de autenticación · A.8.4 Acceso al código fuente |
| T7 | Archivos de `data/consumos/` | **Tampering** / DoS | Un archivo truncado, corrupto o con columnas corridas se carga y **corrompe** meses ya cargados, o tumba `make run` | A (archivos de terceros) | A | El lector cuenta los campos de cada fila (módulo `csv`) y rechaza el archivo **entero** si la estructura está rota; una transacción por archivo con `ROLLBACK`; error de datos de la base → archivo rechazado y la corrida sigue. Probado con un archivo truncado y uno con un valor fuera de rango | Sí | A.8.25 Ciclo de vida de desarrollo seguro · A.8.29 Pruebas de seguridad |
| T8 | Mes nuevo / corrección tardía | **Tampering** | Una lectura vieja (o un archivo reenviado) pisa una corrección ya cargada y cambia lo que se factura | M | M | B3 determinista (versión → archivo con `COLLATE "C"` → fila), decidido en pandas y con el `ON CONFLICT … WHERE` como segunda red; las perdedoras quedan en cuarentena (`SUPERADA_POR_…`). Huella `md5` para comprobar idempotencia | Sí | A.8.32 Gestión de cambios · A.5.33 Protección de los registros |
| T9 | `requirements.txt`, imágenes Docker, proveedor de Terraform | **Tampering** (cadena de suministro) / Elevation | Una dependencia vulnerable (PyYAML 5.3.1: ejecución de código al leer YAML) o una versión que cambia sola introduce código malicioso | M | A | `pip-audit`: 47 → 0 vulnerabilidades; versiones exactas (`==`), imágenes con tag fijo, proveedor de Terraform fijo con `.terraform.lock.hcl` (hashes) en git; `yaml.safe_load` siempre; se quitaron 3 paquetes sin uso | Sí | A.8.8 Gestión de vulnerabilidades técnicas · A.5.21 Seguridad en la cadena de suministro TIC |
| T10 | Postgres (5432) y Redpanda (9092) | **Spoofing** / Information disclosure | Alguien en la misma red Wi-Fi se conecta a la base o al tópico (el prototipo los exponía en `0.0.0.0` con `postgres/postgres`) | M | A | Puertos solo en `127.0.0.1`; superusuario no por defecto; contraseñas SCRAM-SHA-256; MinIO eliminado (menos superficie) | Parcial: Kafka sin TLS ni autenticación (ver riesgos aceptados) | A.8.20 Seguridad de redes · A.8.9 Gestión de la configuración |
| T11 | Tópico `mediciones.raw` | **Spoofing** / Tampering | Un productor no autorizado publica mediciones falsas para disparar o esconder alertas | B (solo local) | M | Solo accesible desde `127.0.0.1`; mensajes inválidos se descartan y se cuentan; el tópico se recrea en cada `make stream` | Parcial: sin SASL/ACL de Kafka | A.8.20 Seguridad de redes · A.8.16 Actividades de monitoreo |
| T12 | `.env` en el host | **Information disclosure** | Quien obtenga el `.env` tiene las contraseñas y las llaves, y con un dump puede leer toda la PII | B | A | `.env` en `.gitignore`; llaves separadas (cifrado / HMAC); en producción irían a KMS y Secrets Manager (`infra/`) | Parcial: en local la llave vive en un archivo | A.8.24 Uso de criptografía · A.5.17 Información de autenticación |

> STRIDE: Spoofing (suplantación), Tampering (alteración), Repudiation (repudio), Information
> disclosure (fuga de información), Denial of service (denegación de servicio), Elevation of
> privilege (escalamiento de privilegios). Probabilidad / Impacto: A = alto, M = medio, B = bajo.

## 4. Riesgos aceptados

| Riesgo | Por qué se acepta | Mitigación parcial |
|---|---|---|
| **Kafka (Redpanda) en PLAINTEXT, sin autenticación** (T10, T11) | Entorno local; agregar TLS + SASL complica `make stream` sin beneficio en `127.0.0.1` | Puerto solo en `127.0.0.1`; en producción: TLS + SASL/SCRAM + ACL por tópico |
| **Un superusuario puede desactivar el trigger de la bitácora** (T5) | En Postgres, el superusuario lo puede todo; es el límite de cualquier control dentro de la misma base | Permisos + trigger cubren a todos los demás; en producción, copia a almacenamiento inmutable (S3 Object Lock / CloudTrail) |
| **La llave de cifrado vive en `.env`** (T12) | No hay KMS en local | Llave aleatoria por instalación, fuera de git y de la base; `infra/kms.tf` describe la solución en AWS |
| **Contraseñas de desarrollo en `.env.example`** | Permiten clonar y correr sin configurar nada (ADR-002) | Solo locales, sin relación con el secreto del prototipo; Postgres solo en `127.0.0.1` |
| **`razon_social` en claro** | Es el nombre de una empresa (Confidencial), no un dato de persona natural; se necesita para operar | No sale en la vista del analista ni en `out/` |
| **2 falsos positivos del patrón de celular en la cuarentena** | Son consumos outlier de 10 dígitos que empiezan por 3 (datos de energía, no PII) | La cuarentena no es visible para el analista; verificado que ningún celular real aparece |

## 5. Qué haría distinto con más tiempo / en producción real

- **Llaves en AWS KMS con envelope encryption** y rotación (descrito en `infra/kms.tf`), y
  contraseñas en **Secrets Manager** con rotación automática.
- **Bitácora replicada a almacenamiento inmutable** (S3 con Object Lock o CloudTrail), fuera del
  alcance del DBA.
- **Kafka con TLS + SASL/SCRAM** y ACL por tópico (solo el productor escribe, solo el consumidor lee).
- **Base administrada (RDS)** en subred privada, cifrada con KMS, sin acceso desde internet.
- **`pip-audit` / Dependabot y escaneo de secretos (gitleaks) en el CI**, en cada cambio.
- **Datos asociados (AAD) en AES-GCM** con el `cliente_id`, para que un valor cifrado no se pueda
  copiar de una fila a otra sin que se detecte.
- **Monitoreo y alertas** sobre la bitácora (corridas con `ERROR`, accesos fuera de horario) y sobre
  la tasa de rechazos.
