# -----------------------------------------------------------------------------
# Bucket S3: donde llegarían los archivos (equivale a la carpeta data/).
#
# Prefijos (carpetas lógicas), uno por zona, para dar permisos por zona:
#   crudos/      archivos tal como llegan (consumos, maestros con PII, facturación)
#   analytics/   salidas sin PII (consumo_mensual.csv, dq_report.json…)
#   auditoria/   copia de la bitácora y los logs (solo se agrega)
#
# Controles [README F]: cifrado con KMS, versionado y sin acceso público.
# -----------------------------------------------------------------------------

resource "aws_s3_bucket" "datos" {
  # El nombre de un bucket es único en TODO AWS: se le agrega el número de cuenta.
  bucket = "${var.proyecto}-${var.entorno}-${data.aws_caller_identity.actual.account_id}"
}

# Cifrado en reposo con NUESTRA llave KMS (no la genérica de AWS).
resource "aws_s3_bucket_server_side_encryption_configuration" "datos" {
  bucket = aws_s3_bucket.datos.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.datos.arn
    }
    bucket_key_enabled = true # menos llamadas a KMS (y menos costo) por objeto
  }
}

# Versionado: si alguien sobrescribe o borra un archivo, la versión anterior queda.
resource "aws_s3_bucket_versioning" "datos" {
  bucket = aws_s3_bucket.datos.id
  versioning_configuration {
    status = "Enabled"
  }
}

# Bloqueo total de acceso público: ni ACLs ni políticas públicas, aunque alguien lo intente.
resource "aws_s3_bucket_public_access_block" "datos" {
  bucket                  = aws_s3_bucket.datos.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Sin ACLs: los permisos se manejan solo con IAM (más simple de auditar).
resource "aws_s3_bucket_ownership_controls" "datos" {
  bucket = aws_s3_bucket.datos.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

# Solo conexiones cifradas (HTTPS): se rechaza cualquier petición sin TLS.
resource "aws_s3_bucket_policy" "datos" {
  bucket = aws_s3_bucket.datos.id
  policy = data.aws_iam_policy_document.bucket.json
}

data "aws_iam_policy_document" "bucket" {
  statement {
    sid       = "SoloConexionesCifradas"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [aws_s3_bucket.datos.arn, "${aws_s3_bucket.datos.arn}/*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}
