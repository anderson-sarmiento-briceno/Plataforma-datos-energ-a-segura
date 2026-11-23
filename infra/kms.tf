# -----------------------------------------------------------------------------
# Llave KMS: la "caja fuerte de llaves" administrada por AWS.
#
# En local, la llave de cifrado está en .env. En AWS estaría aquí, y nunca
# saldría de KMS: el pipeline le pide a KMS una llave de datos (envelope
# encryption: KMS cifra la llave que cifra los datos).
#
#   - Rotación automática cada año.
#   - Política de la llave: SOLO el rol de ingesta puede cifrar, descifrar y
#     generar llaves de datos (igual que en local, donde solo el pipeline
#     tiene la llave). Analista y auditor solo pueden descifrar A TRAVÉS de S3
#     (para leer los objetos de su prefijo), nunca llamar a KMS directamente.
# -----------------------------------------------------------------------------

resource "aws_kms_key" "datos" {
  description             = "Cifra los archivos del pipeline de consumos (${var.entorno})"
  enable_key_rotation     = true
  rotation_period_in_days = 365
  deletion_window_in_days = 30 # si alguien la borra, hay 30 días para arrepentirse
  policy                  = data.aws_iam_policy_document.llave.json
}

resource "aws_kms_alias" "datos" {
  name          = "alias/${var.proyecto}-${var.entorno}"
  target_key_id = aws_kms_key.datos.key_id
}

data "aws_iam_policy_document" "llave" {
  # La cuenta administra la llave (si nadie pudiera administrarla, quedaría inutilizable).
  statement {
    sid       = "AdministracionDeLaCuenta"
    actions   = ["kms:*"]
    resources = ["*"]
    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::${data.aws_caller_identity.actual.account_id}:root"]
    }
  }

  # Ingesta: cifrar, descifrar y generar llaves de datos.
  statement {
    sid       = "UsoPorIngesta"
    actions   = ["kms:Encrypt", "kms:Decrypt", "kms:GenerateDataKey", "kms:DescribeKey"]
    resources = ["*"]
    principals {
      type        = "AWS"
      identifiers = [aws_iam_role.ingesta.arn]
    }
  }

  # Analista y auditor: descifrar SOLO cuando lo pide S3 (al leer un objeto
  # que su política de IAM les permite). No pueden usar la llave por su cuenta.
  statement {
    sid       = "LecturaViaS3"
    actions   = ["kms:Decrypt"]
    resources = ["*"]
    principals {
      type        = "AWS"
      identifiers = [aws_iam_role.analista.arn, aws_iam_role.auditor.arn]
    }
    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["s3.${var.region}.amazonaws.com"]
    }
  }
}
