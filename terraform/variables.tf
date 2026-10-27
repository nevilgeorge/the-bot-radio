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
