variable "aws_region" {
  description = "AWS region"
  type        = string
  default     = "us-east-1"
}

variable "instance_type" {
  description = "EC2 instance type (ARM64)"
  type        = string
  default     = "t4g.small"
}

variable "key_name" {
  description = "SSH key pair name (must exist in AWS)"
  type        = string
}

variable "allowed_ssh_cidr" {
  description = "CIDR allowed to SSH (your IP, e.g. 1.2.3.4/32). Required  no default to prevent accidental exposure."
  type        = string

  validation {
    condition     = var.allowed_ssh_cidr != "0.0.0.0/0"
    error_message = "allowed_ssh_cidr must not be 0.0.0.0/0. Restrict to your IP (e.g. 203.0.113.5/32)."
  }
}
