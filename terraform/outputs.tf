output "data_bucket_name" {
  value = aws_s3_bucket.site["data"].id
}
output "aws_region" {
  value = "us-east-1"
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
