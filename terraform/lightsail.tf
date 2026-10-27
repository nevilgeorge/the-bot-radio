resource "aws_lightsail_key_pair" "site" {
  name       = "${var.name_prefix}-operator"
  public_key = trimspace(var.ssh_public_key)
}

resource "aws_lightsail_instance" "site" {
  name              = "${var.name_prefix}-app"
  availability_zone = var.availability_zone
  blueprint_id      = "ubuntu_24_04"
  bundle_id         = var.lightsail_bundle_id
  ip_address_type   = "ipv4"
  key_pair_name     = aws_lightsail_key_pair.site.name
  user_data         = file("${path.module}/templates/bootstrap.sh")
}

resource "aws_lightsail_static_ip" "site" {
  name = "${var.name_prefix}-site"
}

resource "aws_lightsail_static_ip_attachment" "site" {
  static_ip_name = aws_lightsail_static_ip.site.name
  instance_name  = aws_lightsail_instance.site.name

  # Lightsail detaches the IP when an instance is replaced, even if its name is reused.
  lifecycle {
    replace_triggered_by = [aws_lightsail_instance.site]
  }
}

resource "aws_lightsail_instance_public_ports" "site" {
  instance_name = aws_lightsail_instance.site.name

  # A replacement instance starts with default ports; reapply the managed firewall.
  lifecycle {
    replace_triggered_by = [aws_lightsail_instance.site]
  }
  port_info {
    protocol  = "tcp"
    from_port = 22
    to_port   = 22
    cidrs     = var.ssh_allowed_cidrs
  }
  port_info {
    protocol  = "tcp"
    from_port = 80
    to_port   = 80
    cidrs     = ["0.0.0.0/0"]
  }
  port_info {
    protocol  = "tcp"
    from_port = 443
    to_port   = 443
    cidrs     = ["0.0.0.0/0"]
  }
}
