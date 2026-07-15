variable "region" {
  description = "Alibaba Cloud region, for example ap-southeast-1."
  type        = string
  default     = "ap-southeast-1"
}

variable "zone" {
  description = "Availability zone in the selected region."
  type        = string
  default     = "ap-southeast-1a"
}

variable "instance_type" {
  description = "ECS instance type; adjust to availability in your account."
  type        = string
  default     = "ecs.e-c1m2.large"
}

variable "ssh_public_key" {
  description = "OpenSSH public key used to access the demo host."
  type        = string
}

variable "project_name" {
  type    = string
  default = "directorgraph"
}
