# -----------------------------------------------------------------------------
# Roles IAM con menor privilegio: los mismos tres roles de Postgres (C4).
#
#   Rol        | Postgres (local)                    | AWS (aquí)
#   -----------|-------------------------------------|----------------------------------------
#   ingesta    | escribe en core/cuarentena/control; | lee crudos/, escribe analytics/,
#              | solo AGREGA a la bitácora           | solo AGREGA a auditoria/; usa la llave
#   analista   | solo SELECT en la vista sin PII     | solo lee analytics/ (sin PII)
#   auditor    | solo SELECT en la bitácora          | solo lee auditoria/
#
# Nadie puede borrar objetos (s3:DeleteObject no se da a ningún rol).
# -----------------------------------------------------------------------------

locals {
  bucket = aws_s3_bucket.datos.arn
}

# ---------- Quién puede "ponerse" cada rol (políticas de confianza) ----------

# Ingesta la asume el servicio que correría el pipeline (una tarea de ECS),
# no una persona.
data "aws_iam_policy_document" "confianza_servicio" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

# Analista y auditor son personas de la cuenta, y deben haber entrado con MFA.
data "aws_iam_policy_document" "confianza_personas" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::${data.aws_caller_identity.actual.account_id}:root"]
    }
    condition {
      test     = "Bool"
      variable = "aws:MultiFactorAuthPresent"
      values   = ["true"]
    }
  }
}

# ---------- ingesta ----------

resource "aws_iam_role" "ingesta" {
  name               = "${var.proyecto}-${var.entorno}-ingesta"
  assume_role_policy = data.aws_iam_policy_document.confianza_servicio.json
}

data "aws_iam_policy_document" "ingesta" {
  statement {
    sid       = "ListarSoloSusPrefijos"
    actions   = ["s3:ListBucket"]
    resources = [local.bucket]
    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["crudos/*", "analytics/*", "auditoria/*"]
    }
  }
  statement {
    sid       = "LeerCrudos"
    actions   = ["s3:GetObject"]
    resources = ["${local.bucket}/crudos/*"]
  }
  statement {
    sid       = "EscribirSalidas"
    actions   = ["s3:PutObject"]
    resources = ["${local.bucket}/analytics/*"]
  }
  # Bitácora: solo AGREGAR (PutObject). Sin Get, sin Delete: como el INSERT de Postgres.
  statement {
    sid       = "AgregarAuditoria"
    actions   = ["s3:PutObject"]
    resources = ["${local.bucket}/auditoria/*"]
  }
  statement {
    sid       = "UsarLaLlave"
    actions   = ["kms:Encrypt", "kms:Decrypt", "kms:GenerateDataKey"]
    resources = [aws_kms_key.datos.arn]
  }
}

resource "aws_iam_role_policy" "ingesta" {
  name   = "permisos-minimos"
  role   = aws_iam_role.ingesta.id
  policy = data.aws_iam_policy_document.ingesta.json
}

# ---------- analista ----------

resource "aws_iam_role" "analista" {
  name               = "${var.proyecto}-${var.entorno}-analista"
  assume_role_policy = data.aws_iam_policy_document.confianza_personas.json
}

data "aws_iam_policy_document" "analista" {
  statement {
    sid       = "ListarSoloAnalytics"
    actions   = ["s3:ListBucket"]
    resources = [local.bucket]
    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["analytics/*"]
    }
  }
  # Solo las salidas sin PII. Nada de crudos/ (tiene PII) ni de auditoria/.
  statement {
    sid       = "LeerAnalytics"
    actions   = ["s3:GetObject"]
    resources = ["${local.bucket}/analytics/*"]
  }
}

resource "aws_iam_role_policy" "analista" {
  name   = "permisos-minimos"
  role   = aws_iam_role.analista.id
  policy = data.aws_iam_policy_document.analista.json
}

# ---------- auditor ----------

resource "aws_iam_role" "auditor" {
  name               = "${var.proyecto}-${var.entorno}-auditor"
  assume_role_policy = data.aws_iam_policy_document.confianza_personas.json
}

data "aws_iam_policy_document" "auditor" {
  statement {
    sid       = "ListarSoloAuditoria"
    actions   = ["s3:ListBucket"]
    resources = [local.bucket]
    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["auditoria/*"]
    }
  }
  statement {
    sid       = "LeerAuditoria"
    actions   = ["s3:GetObject"]
    resources = ["${local.bucket}/auditoria/*"]
  }
}

resource "aws_iam_role_policy" "auditor" {
  name   = "permisos-minimos"
  role   = aws_iam_role.auditor.id
  policy = data.aws_iam_policy_document.auditor.json
}
