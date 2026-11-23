# ===========================================================================
# Operación de la plataforma. Flujo completo:
#   make setup && make up && make run && make stream && make test
# ===========================================================================

.PHONY: setup up run stream test down clean validar-infra

# Python del entorno virtual: el python3 del sistema no tiene las librerías.
PY := .venv/bin/python

# Variables de .env disponibles para los comandos (Paso 7: make up necesita
# los usuarios y contraseñas de los roles). "-include": si .env todavía no
# existe (antes de make setup), no falla.
-include .env
export

## Instala dependencias. Debe ser idempotente y no requerir interaccion.
# Cada línea revisa antes de actuar, así correrlo dos veces no rompe nada.
# .env solo se crea si no existe: nunca se pisa uno que ya tenga valores.
#
# Llaves de cifrado (Paso 7): NO están en .env.example ni en git. Si .env no
# tiene una llave, se genera una AL AZAR (32 bytes = 256 bits, en base64) con
# el módulo secrets de Python. Nadie necesita conocerlas: solo las usa
# el pipeline. Si ya existe, no se toca (cambiarla
# dejaría ilegible lo que ya se cifró).
NUEVA_LLAVE = $(PY) -c "import base64, secrets; print(base64.b64encode(secrets.token_bytes(32)).decode())"
setup:
	@test -d .venv || python3 -m venv .venv
	.venv/bin/pip install -q -r requirements.txt
	@test -f .env || (cp .env.example .env && echo "Se creo .env desde .env.example")
	@grep -q '^LLAVE_CIFRADO=.' .env || (echo "LLAVE_CIFRADO=$$($(NUEVA_LLAVE))" >> .env && echo "Se genero LLAVE_CIFRADO en .env")
	@grep -q '^LLAVE_HMAC=.' .env || (echo "LLAVE_HMAC=$$($(NUEVA_LLAVE))" >> .env && echo "Se genero LLAVE_HMAC en .env")

## Levanta la infraestructura local y espera a que este lista para recibir
## conexiones. Al terminar, 'make run' debe poder correr sin pasos manuales.
# --wait espera a que los healthchecks de docker-compose.yml pasen.
# Luego aplica sql/*.sql en orden con el psql que trae el contenedor (no hace
# falta instalar psql en el host). Las comillas simples hacen que
# $POSTGRES_USER se expanda DENTRO del contenedor y no en el host.
# ON_ERROR_STOP=1: si un archivo SQL falla, make up falla (no sigue a medias).
# client-min-messages=warning: oculta los NOTICE "already exists, skipping"
# del IF NOT EXISTS en la segunda corrida (son normales, solo ensucian).
# -e VARIABLE (sin "=valor"): pasa al contenedor el valor que ya está en el
# entorno, SIN escribirlo en la línea de comandos (no aparece en `ps`).
# 04_roles_permisos.sql las lee con \getenv.
VARIABLES_ROLES := -e PG_ROL_INGESTA_USER -e PG_ROL_INGESTA_PASSWORD \
                   -e PG_ROL_ANALISTA_USER -e PG_ROL_ANALISTA_PASSWORD \
                   -e PG_ROL_AUDITOR_USER -e PG_ROL_AUDITOR_PASSWORD
up:
	docker compose up -d --wait
	@for f in sql/*.sql; do \
		[ -e "$$f" ] || continue; \
		echo "Aplicando $$f"; \
		docker compose exec -T -e PGOPTIONS='--client-min-messages=warning' $(VARIABLES_ROLES) postgres sh -c 'psql -q -v ON_ERROR_STOP=1 -U "$$POSTGRES_USER" -d "$$POSTGRES_DB"' < "$$f" || exit 1; \
	done

## Ejecuta el pipeline batch completo de punta a punta.
## DEBE ser idempotente: correrlo dos veces seguidas no puede duplicar datos
## ni cambiar los resultados.
## Al terminar deben existir:
##   out/consumo_mensual.csv
##   out/dq_report.json
##   out/metrics.json
##   logs/pipeline.log
run:
	$(PY) -m src.pipeline

## Procesa el stream de mediciones y termina solo (no puede quedarse colgado).
## Al terminar debe existir: out/alertas.json
# 1) tópico vacío (correrlo 2 veces no repite mediciones), 2) el productor
# publica todo y termina (--rate 0 = sin pausas), 3) el consumidor lee,
# aplica las 3 reglas y termina solo tras unos segundos sin mensajes.
stream:
	$(PY) -m src.stream_consumidor --preparar-topico
	$(PY) tools/producer.py --rate 0
	$(PY) -m src.stream_consumidor

## Corre tu suite de pruebas. Exit code 0 = todo paso.
# "python -m pytest" (y no "pytest") agrega la raíz del proyecto al path, así
# las pruebas pueden hacer "from src.lectores import ...".
test:
	$(PY) -m pytest -q

## Apaga la infraestructura.
# Sin -v: los datos de Postgres se conservan para el siguiente 'make up'.
down:
	docker compose down

## Borra artefactos generados (out/, logs/, volumenes).
clean:
	docker compose down -v --remove-orphans
	rm -rf out logs

## Revisa que infra/ (Terraform) esté bien escrito.
## No despliega nada ni necesita cuenta de AWS: -backend=false no guarda estado
## remoto y validate no se conecta a AWS. fmt -check revisa el formato estándar.
validar-infra:
	cd infra && terraform init -backend=false -input=false >/dev/null && terraform validate && terraform fmt -check
