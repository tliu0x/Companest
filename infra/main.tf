terraform {
  required_version = ">= 1.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.30"
    }
  }
}

provider "aws" {
  region = var.aws_region
}

#  Latest Ubuntu 24.04 ARM64 AMI 
data "aws_ami" "ubuntu" {
  most_recent = true
  owners      = ["099720109477"] # Canonical

  filter {
    name   = "name"
    values = ["ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-arm64-server-*"]
  }

  filter {
    name   = "architecture"
    values = ["arm64"]
  }
}

#  Security Group 
resource "aws_security_group" "companest" {
  name        = "companest-sg"
  description = "Companest all-in-one: SSH only"

  # SSH
  ingress {
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = [var.allowed_ssh_cidr]
    description = "SSH"
  }

  # All outbound (apt, Docker Hub, Claude API, Telegram API)
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "companest-sg" }
}

#  EC2 Instance (Companest + LiteLLM) 
resource "aws_instance" "companest" {
  ami                    = data.aws_ami.ubuntu.id
  instance_type          = var.instance_type
  key_name               = var.key_name
  vpc_security_group_ids = [aws_security_group.companest.id]

  root_block_device {
    volume_size = 20
    volume_type = "gp3"
  }

  user_data = file("${path.module}/user-data.sh")

  tags = { Name = "companest-all-in-one" }
}

#  Elastic IP (survives stop/start) 
resource "aws_eip" "companest" {
  instance = aws_instance.companest.id
  tags     = { Name = "companest-eip" }
}
