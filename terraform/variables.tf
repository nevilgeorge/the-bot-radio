variable "name_prefix" {
  description = "Existing data bucket prefix; keep unchanged during migration."
  type        = string
  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9-]{2,45}[a-z0-9]$", var.name_prefix))
    error_message = "Use 4–47 lowercase letters, numbers or hyphens, beginning and ending alphanumeric."
  }
}

variable "ssh_public_key" {
  description = "Operator SSH public key contents (never the private key)."
  type        = string
  validation {
    condition     = can(regex("^ssh-(ed25519|rsa) [A-Za-z0-9+/=]+", trimspace(var.ssh_public_key)))
    error_message = "Supply an OpenSSH ed25519 or RSA public key."
  }
}

variable "ssh_allowed_cidrs" {
  description = "Required operator IPv4 CIDRs allowed to SSH; unrestricted SSH is rejected."
  type        = set(string)
  validation {
    condition     = length(var.ssh_allowed_cidrs) > 0 && alltrue([for cidr in var.ssh_allowed_cidrs : can(cidrnetmask(cidr)) && !endswith(cidr, "/0")])
    error_message = "Supply at least one IPv4 CIDR narrower than /0."
  }
}

variable "lightsail_bundle_id" {
  description = "Lightsail Linux bundle; small_3_0 provides 2 GB RAM."
  type        = string
  default     = "small_3_0"
}

variable "availability_zone" {
  description = "Lightsail availability zone in us-east-1."
  type        = string
  default     = "us-east-1a"
  validation {
    condition     = can(regex("^us-east-1[a-f]$", var.availability_zone))
    error_message = "Choose an availability zone in us-east-1."
  }
}

variable "generation_image_uri" {
  description = "Immutable generator ECR digest URI; null stages foundation resources only."
  type        = string
  default     = null
  nullable    = true
  validation {
    condition = var.generation_image_uri == null ? true : (
      startswith(var.generation_image_uri, "${aws_ecr_repository.generation.repository_url}@sha256:") &&
      can(regex("@sha256:[a-f0-9]{64}$", var.generation_image_uri))
    )
    error_message = "Supply a sha256 digest URI from this configuration's generator ECR repository."
  }
}

variable "generation_schedule_enabled" {
  description = "Enable daily 06:00 America/New_York generation after end-to-end validation; duplicate delivery risk must be accepted until protection is implemented."
  type        = bool
  default     = false
  validation {
    condition     = !var.generation_schedule_enabled || var.generation_image_uri != null
    error_message = "A generator image digest URI is required before enabling the schedule."
  }
}
