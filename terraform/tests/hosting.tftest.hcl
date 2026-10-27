mock_provider "aws" {
  mock_resource "aws_s3_bucket" {
    defaults = {
      arn = "arn:aws:s3:::mock-bucket"
    }
  }
  mock_resource "aws_lightsail_static_ip" {
    defaults = {
      ip_address = "203.0.113.42"
    }
  }
}

variables {
  name_prefix       = "the-bot-radio-test"
  ssh_public_key    = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITestKey"
  ssh_allowed_cidrs = ["198.51.100.7/32"]
}

run "lightsail_hosting" {
  command = apply

  assert {
    condition     = aws_lightsail_instance.site.blueprint_id == "ubuntu_24_04" && aws_lightsail_instance.site.bundle_id == "small_3_0" && aws_lightsail_instance.site.ip_address_type == "ipv4" && aws_lightsail_instance.site.availability_zone == "us-east-1a"
    error_message = "Default hosting must be Ubuntu 24.04, 2 GB, IPv4 in us-east-1."
  }
  assert {
    condition     = aws_lightsail_instance.site.key_pair_name == aws_lightsail_key_pair.site.name && aws_lightsail_key_pair.site.public_key == var.ssh_public_key
    error_message = "Import and attach the operator public key."
  }
  assert {
    condition     = length(toset([aws_lightsail_instance.site.name, aws_lightsail_static_ip.site.name, aws_lightsail_key_pair.site.name])) == 3
    error_message = "Lightsail resource names must be unique across resource types."
  }
  assert {
    condition     = aws_lightsail_static_ip_attachment.site.static_ip_name == aws_lightsail_static_ip.site.name && aws_lightsail_static_ip_attachment.site.instance_name == aws_lightsail_instance.site.name
    error_message = "Static IPv4 must be attached to the site instance."
  }
  assert {
    condition = length(aws_lightsail_instance_public_ports.site.port_info) == 3 && alltrue([
      for port in aws_lightsail_instance_public_ports.site.port_info : port.protocol == "tcp" && port.from_port == port.to_port && (
        port.from_port == 22 ? port.cidrs == var.ssh_allowed_cidrs : contains([80, 443], port.from_port) && contains(port.cidrs, "0.0.0.0/0")
      )
    ])
    error_message = "Only SSH from the allowed CIDRs and public HTTP/HTTPS may be open."
  }
  assert {
    condition = alltrue([for expected in [
      "uv python install 3.13", "User=bot-radio", "Restart=on-failure", "StandardOutput=journal",
      "BOT_RADIO_DATA_DIR=/var/lib/the-bot-radio/data", "--host 127.0.0.1", "--forwarded-allow-ips=127.0.0.1",
      "thebotradio.com, www.thebotradio.com", "reverse_proxy 127.0.0.1:8000", "bootstrap-complete"
    ] : strcontains(aws_lightsail_instance.site.user_data, expected)]) && !strcontains(aws_lightsail_instance.site.user_data, "--reload")
    error_message = "Bootstrap must install Python and configure persistent data, restricted Uvicorn, systemd and both Caddy domains."
  }
  assert {
    condition     = output.static_ip_address == "203.0.113.42" && output.ssh_host == output.static_ip_address && output.ssh_user == "ubuntu" && output.instance_name == aws_lightsail_instance.site.name
    error_message = "Deployment must receive the stable SSH address and Ubuntu user."
  }
  assert {
    condition     = output.porkbun_apex_record.type == "A" && output.porkbun_apex_record.host == "" && output.porkbun_apex_record.value == output.static_ip_address && output.porkbun_www_record.type == "CNAME" && output.porkbun_www_record.value == "thebotradio.com"
    error_message = "Porkbun must point apex A to static IPv4 and www CNAME to apex."
  }
  assert {
    condition     = length(aws_s3_bucket.site) == 1 && !aws_s3_bucket.site["data"].force_destroy && output.data_bucket_name == aws_s3_bucket.site["data"].id
    error_message = "Keep the existing data bucket address and refuse nonempty deletion."
  }
  assert {
    condition     = aws_s3_bucket_public_access_block.site["data"].block_public_acls && aws_s3_bucket_public_access_block.site["data"].block_public_policy && aws_s3_bucket_public_access_block.site["data"].ignore_public_acls && aws_s3_bucket_public_access_block.site["data"].restrict_public_buckets
    error_message = "Retain all data-bucket public access protections."
  }
  assert {
    condition     = aws_s3_bucket_versioning.site["data"].versioning_configuration[0].status == "Enabled" && one(aws_s3_bucket_server_side_encryption_configuration.site["data"].rule).apply_server_side_encryption_by_default[0].sse_algorithm == "AES256" && aws_s3_bucket_ownership_controls.site["data"].rule[0].object_ownership == "BucketOwnerEnforced"
    error_message = "Retain versioning, encryption and ownership protections."
  }
  assert {
    condition     = length(jsondecode(aws_s3_bucket_policy.site["data"].policy).Statement) == 1 && jsondecode(aws_s3_bucket_policy.site["data"].policy).Statement[0].Sid == "DenyInsecureTransport" && jsondecode(aws_s3_bucket_policy.site["data"].policy).Statement[0].Effect == "Deny"
    error_message = "The data bucket must have only the TLS deny policy, with no access grants."
  }
}

run "custom_bundle_and_cidrs" {
  command = plan
  variables {
    lightsail_bundle_id = "medium_3_0"
    ssh_allowed_cidrs   = ["198.51.100.0/24", "203.0.113.1/32"]
  }
  assert {
    condition     = aws_lightsail_instance.site.bundle_id == "medium_3_0" && one([for port in aws_lightsail_instance_public_ports.site.port_info : port if port.from_port == 22]).cidrs == var.ssh_allowed_cidrs
    error_message = "Allow configurable bundles and restricted SSH CIDRs."
  }
}

run "reject_public_ssh" {
  command = plan
  variables {
    ssh_allowed_cidrs = ["0.0.0.0/0"]
  }
  expect_failures = [var.ssh_allowed_cidrs]
}

run "reject_empty_ssh" {
  command = plan
  variables {
    ssh_allowed_cidrs = []
  }
  expect_failures = [var.ssh_allowed_cidrs]
}
