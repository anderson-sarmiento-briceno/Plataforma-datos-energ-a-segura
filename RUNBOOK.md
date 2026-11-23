# Runbook operativo

Para quien esté de turno a las 2 a.m. Corto: síntoma → dónde mirar → qué hacer.

## Cómo corre normalmente

| | |
|---|---|
| **Comando** | `make run` (`python -m src.pipeline`). Requiere `make up` antes (Postgres y Redpanda arriba) |
| **Frecuencia** | Cuando llega un archivo nuevo a `data/consumos/` o `data/facturacion/` (o a diario). Es **idempotente**: correrlo de más no daña nada |
| **Duración** | ~7 s con los 4 meses desde cero; ~1 s si no hay archivos nuevos (todo se salta por hash) |
| **Usuario de BD** | `rol_ingesta` (nunca el superusuario) |
| **Deja** | `out/consumo_mensual.csv`, `out/dq_report.json`, `out/metrics.json`, `logs/pipeline.log` (JSON, sin PII) y 5 filas en `audit.bitacora_corridas` (una por etapa + la corrida) |
| **Código de salida** | `0` = bien (incluso con archivos rechazados, que van a cuarentena). `1` = falló algo que impide seguir (p. ej. la base no responde) |

Qué mirar primero, siempre:
```bash
tail -n 20 logs/pipeline.log                       # eventos JSON de la última corrida
cat out/metrics.json                               # duración por etapa y rechazos por motivo
docker compose exec postgres psql -U energia_admin -d energia_datos -c \
  "SELECT etapa, resultado, tipo_error, inicio, filas_leidas, filas_cargadas, filas_rechazadas
   FROM audit.bitacora_corridas ORDER BY id DESC LIMIT 10;"
```

## Señales de que algo anda mal

| Síntoma | Dónde se ve | Causa probable |
|---|---|---|
| `make run` termina con exit 1 | Consola; log con `"evento": "fin_corrida", "resultado": "ERROR"`; bitácora con `resultado = ERROR` y `tipo_error` | La base no responde (`OperationalError`), falta una variable o llave en `.env` (`RuntimeError`) |
| `make up` falla o queda esperando | Consola de `make up` | Docker Desktop apagado; puerto 5432 ocupado por otro Postgres; error en un `sql/*.sql` (`ON_ERROR_STOP`) |
| Un archivo no aparece en el reporte | `cuarentena.archivo_rechazado`; log con `"estado": "RECHAZADO"` | Archivo truncado o corrupto (`FILAS_INCOMPLETAS`, `FILAS_CON_CAMPOS_DE_MAS`, `NO_PARSEABLE`, `COLUMNAS_FALTANTES`, `JSON_INVALIDO`) o un dato que la base rechazó (`ERROR_CARGA:<tipo>`) |
| Sube mucho `filas_rechazadas_por_motivo` | `out/metrics.json` (bloque `acumulado`), `cuarentena.consumo_rechazado` | Cambió el formato de origen (unidad, hora, separador), un medidor reporta basura, o llegó una frontera nueva sin maestro (`FRONTERA_DESCONOCIDA`) |
| `horas_sin_lectura_valida_total` sube | `out/dq_report.json` | Medidores sin reportar o lecturas descartadas por B4 |
| `InvalidTag` al descifrar | Al leer PII con la llave | `LLAVE_CIFRADO` de `.env` no es la que cifró los datos (se regeneró o se perdió) |

## Procedimientos

### El pipeline falló a mitad de camino
**Es seguro reintentar.** Cada archivo se carga en **una sola transacción**: si falló, se hizo
`ROLLBACK` y no quedó nada a medias. Los archivos ya cargados se saltan por su huella SHA-256.
1. Mirar el `tipo_error` en el log o en la bitácora (nunca trae datos, solo el tipo).
2. Si es la base (`OperationalError`): `docker compose ps`; si está caída, `make up`.
3. Si falta una variable o llave (`RuntimeError`): revisar `.env`. **No regenerar `LLAVE_CIFRADO`**
   si ya hay datos cifrados (se volverían ilegibles); `make setup` solo crea las que faltan.
4. Reintentar con `make run` y confirmar `fin_corrida` con `"resultado": "OK"`.

### Llegó un archivo rechazado (truncado, corrupto o con un dato que la base no acepta)
- No hay que hacer nada urgente: los demás archivos se cargaron y los meses anteriores no cambiaron.
- Ver el motivo: `SELECT * FROM cuarentena.archivo_rechazado ORDER BY registrado_en DESC;`
- Pedir al origen el archivo corregido y dejarlo en `data/consumos/` **con el mismo nombre**. En la
  siguiente corrida su huella es distinta y se carga. Si ese nombre ya estaba cargado, primero se
  borra lo que venía de él (hecho y cuarentena) y se recarga, en la misma transacción.

### Llegaron datos corregidos de un mes ya cerrado
- Si vienen como **archivo nuevo** (como `consumos_2026-04_correccion.csv`): basta con dejarlo en
  `data/consumos/` y correr `make run`. Gana la lectura de **versión mayor**; si empatan, la del
  archivo posterior en orden de nombre y luego la fila posterior (B3). Las reemplazadas se cuentan
  en `correcciones_tardias_aplicadas`; las que pierden quedan en cuarentena
  (`SUPERADA_POR_VERSION_MAYOR` / `SUPERADA_POR_DESEMPATE`).
- Si llega el **mismo archivo corregido**: ver el procedimiento anterior.
- Verificar con `scripts/huella.sql` antes y después: solo deben cambiar las filas corregidas.

### La tasa de rechazo se disparó
1. Ver qué motivo creció: `out/metrics.json` → `acumulado.filas_rechazadas_por_motivo`, o
   `SELECT archivo_origen, motivo, count(*) FROM cuarentena.consumo_rechazado GROUP BY 1, 2 ORDER BY 3 DESC;`
2. Mirar unas filas de ese motivo (`ts_texto`, `valor_texto`, `unidad`: es la fila tal como llegó).
3. Según el motivo:
   - `HORA_INVALIDA` o `UNIDAD_DESCONOCIDA` en un archivo entero → **cambió el formato** del origen. No
     forzar la carga: confirmar con el proveedor y, si el formato es legítimo, ajustar `src/lectores.py`
     con una prueba nueva.
   - `FRONTERA_DESCONOCIDA` → falta la frontera en `fronteras.csv`. Actualizar el maestro y recargar.
   - `OUTLIER` / `NEGATIVO` concentrados en una frontera → avisar a medición (posible medidor dañado).
4. Los datos rechazados **no se borran**: se pueden reprocesar cuando se corrija la causa.

### Sospecha de acceso indebido a PII
**Primeros 15 minutos:**
1. **Contener:** si hay un usuario comprometido, bloquearlo sin borrar evidencia:
   `ALTER ROLE <rol> NOLOGIN;` (como `energia_admin`).
2. **Mirar:**
   - `audit.bitacora_corridas`: corridas fuera de horario o con un `usuario_bd` inesperado.
   - `SELECT usename, client_addr, backend_start, query FROM pg_stat_activity;` → sesiones activas.
   - Los permisos siguen como deben: `make test` (incluye `tests/test_permisos.py`).
   - Logs y `out/` no tienen PII: buscar correos, celulares y cédulas con las expresiones de `src/pii.py`.
3. **Avisar** al responsable de seguridad de la información y al oficial de protección de datos
   (Ley 1581: un incidente con datos personales puede tener que reportarse a la SIC).

**Qué NO hacer:**
- No borrar ni editar la bitácora ni los logs (la bitácora no lo permite; los logs son evidencia).
- No regenerar `LLAVE_CIFRADO` en caliente (se pierde el acceso a la PII ya cifrada). Si la llave
  se filtró: crear una nueva, **descifrar con la vieja y volver a cifrar** con la nueva, y recién
  entonces destruir la vieja.
- No copiar datos a equipos personales "para revisar".

**Contexto:** la PII está cifrada con AES-256-GCM y la llave no está en la base: un acceso a la base
(incluso como superusuario) solo expone bytes cifrados y `nit_hash`. El riesgo real es el acceso al
`.env` del servidor.

## Contactos y escalamiento

| Situación | A quién | Cuándo |
|---|---|---|
| Falla técnica del pipeline o de la base | Ingeniero de datos de turno | Si no se resuelve reintentando en 30 min |
| Archivo de origen corrupto o con formato nuevo | Proveedor del archivo / equipo de medición | En horario laboral (no bloquea los demás meses) |
| Sospecha de acceso indebido a PII | Responsable de seguridad de la información + oficial de protección de datos | **De inmediato** |

_(Los roles son genéricos: cada equipo asigna nombres y canales de contacto.)_
