# Datos configurables. Ninguno es secreto.

variable "region" {
  description = "Región de AWS"
  type        = string
  default     = "us-east-1"
}

variable "proyecto" {
  description = "Prefijo de los nombres de los recursos"
  type        = string
  default     = "energia-consumos"
}

variable "entorno" {
  description = "Entorno (evaluacion, produccion…)"
  type        = string
  default     = "evaluacion"
}
