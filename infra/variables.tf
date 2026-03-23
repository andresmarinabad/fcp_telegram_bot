variable "project_id" {
  description = "Project ID"
  type        = string
}

variable "region" {
  description = "Region"
  type        = string
  default     = "us-central1"
}

variable "github_owner" {
  type = string
  description = "GitHub repo owner"
}

variable "github_token" {
  type = string
  description = "Token de GitHub"
}

variable "telegram_bot_token" { 
  type      = string 
  sensitive = true 
}