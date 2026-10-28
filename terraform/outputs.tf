output "data_bucket_name" {
  value = aws_s3_bucket.site["data"].id
}
output "aws_region" {
  value = "us-east-1"
}
output "application_s3_environment" {
  value = local.application_s3_environment
}
output "web_iam_user_arn" {
  value = aws_iam_user.web.arn
}
output "instance_name" {
  value = aws_lightsail_instance.site.name
}
output "static_ip_address" {
  value = aws_lightsail_static_ip.site.ip_address
}
output "ssh_host" {
  value = aws_lightsail_static_ip.site.ip_address
}
output "ssh_user" {
  value = "ubuntu"
}
output "ssh_command" {
  description = "Add -i /path/to/private-key as needed; verify the host key on first connection."
  value       = "ssh ubuntu@${aws_lightsail_static_ip.site.ip_address}"
}
output "porkbun_apex_record" {
  description = "Create after deploying and checking the instance through an SSH tunnel."
  value = {
    type  = "A"
    host  = ""
    value = aws_lightsail_static_ip.site.ip_address
  }
}
output "porkbun_www_record" {
  value = {
    type  = "CNAME"
    host  = "www"
    value = "thebotradio.com"
  }
}

output "generation_repository_url" {
  value = aws_ecr_repository.generation.repository_url
}
output "generation_secret_arn" {
  value = aws_secretsmanager_secret.generation.arn
}
output "generation_cluster_arn" {
  value = aws_ecs_cluster.generation.arn
}
output "generation_task_definition_arn" {
  value = try(aws_ecs_task_definition.generation[0].arn, null)
}
output "generation_subnet_ids" {
  value = [for subnet in aws_subnet.generation : subnet.id]
}
output "generation_security_group_id" {
  value = aws_security_group.generation.id
}
output "generation_schedule_name" {
  value = try(aws_scheduler_schedule.generation[0].name, null)
}
output "generation_log_group_name" {
  value = aws_cloudwatch_log_group.generation.name
}
output "generation_dead_letter_queue_url" {
  value = aws_sqs_queue.generation_dead_letter.url
}
