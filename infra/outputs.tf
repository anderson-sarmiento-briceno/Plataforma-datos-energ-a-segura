# Lo que Terraform "devolvería" al desplegar: nombres y ARN (no son secretos).

output "bucket" {
  description = "Bucket de datos (crudos/, analytics/, auditoria/)"
  value       = aws_s3_bucket.datos.bucket
}

output "llave_kms" {
  description = "ARN de la llave KMS que cifra el bucket"
  value       = aws_kms_key.datos.arn
}

output "roles" {
  description = "ARN de los roles de menor privilegio (equivalentes a C4)"
  value = {
    ingesta  = aws_iam_role.ingesta.arn
    analista = aws_iam_role.analista.arn
    auditor  = aws_iam_role.auditor.arn
  }
}
