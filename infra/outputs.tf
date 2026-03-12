output "instance_id" {
  description = "EC2 instance ID"
  value       = aws_instance.companest.id
}

output "public_ip" {
  description = "Elastic IP (fixed, survives stop/start)"
  value       = aws_eip.companest.public_ip
}

output "ssh_command" {
  description = "SSH into the instance"
  value       = "ssh -i ~/.ssh/${var.key_name}.pem ubuntu@${aws_eip.companest.public_ip}"
}
