# Crear el bucket para el state
resource "google_storage_bucket" "tofu_state" {
  name          = "${var.project_id}-tfstate" # El nombre debe ser único globalmente
  location      = var.region              # Región del Free Tier
  force_destroy = false                       # Evita borrar el bucket si tiene datos
  storage_class = "STANDARD"

  # Versionamiento para poder recuperar estados anteriores
  versioning {
    enabled = true
  }

  # Bloqueo de acceso público por seguridad
  public_access_prevention = "enforced"
}