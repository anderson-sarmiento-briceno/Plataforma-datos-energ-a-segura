# -----------------------------------------------------------------------------
# Infraestructura en AWS para el pipeline de consumos (Parte F).
#
# No está desplegado ([README F]): solo describe cómo correría esto en AWS. Se
# comprueba con `terraform init -backend=false && terraform validate`
# (o `make validar-infra`), sin cuenta de AWS ni credenciales.
#
# Equivalencias con la solución local:
#   data/                    → bucket S3 (s3.tf), cifrado con KMS y sin acceso público
#   LLAVE_CIFRADO en .env    → llave KMS con rotación (kms.tf)
#   rol_ingesta / analista / → roles IAM con los mismos permisos mínimos (iam.tf)
#   auditor de Postgres (C4)
# -----------------------------------------------------------------------------

terraform {
  required_version = ">= 1.5"

  required_providers {
    aws = {
      source = "hashicorp/aws"
      # Versión exacta (como requirements.txt con ==). El archivo
      # .terraform.lock.hcl fija además su hash y SÍ va a git.
      version = "6.71.0"
    }
  }
}

provider "aws" {
  region = var.region

  # Etiquetas en TODOS los recursos: quién es el dueño y qué datos tiene.
  default_tags {
    tags = {
      proyecto       = var.proyecto
      entorno        = var.entorno
      clasificacion  = "Restringido-PII" # el bucket recibe archivos con datos personales
      gestionado_por = "terraform"
    }
  }
}

# Cuenta en la que se desplegaría: se usa para nombres únicos y políticas.
# (terraform validate no la consulta: no necesita credenciales.)
data "aws_caller_identity" "actual" {}
