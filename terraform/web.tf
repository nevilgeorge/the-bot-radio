# Lightsail has no instance profile. Create access keys separately so their secrets
# never enter Terraform configuration, outputs, plans, or state.
resource "aws_iam_user" "web" {
  name          = "${var.name_prefix}-web"
  force_destroy = false
}

resource "aws_iam_user_policy" "web" {
  name = "read-daily-records"
  user = aws_iam_user.web.name
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "ListDailyRecords"
        Effect    = "Allow"
        Action    = ["s3:ListBucket"]
        Resource  = aws_s3_bucket.site["data"].arn
        Condition = { StringEquals = { "s3:prefix" = "production/days/" } }
      },
      {
        Sid      = "ReadDailyRecords"
        Effect   = "Allow"
        Action   = ["s3:GetObject"]
        Resource = "${aws_s3_bucket.site["data"].arn}/production/days/*"
      }
    ]
  })
}

output "web_iam_user_name" {
  description = "Read-only website IAM user; manage access keys outside Terraform."
  value       = aws_iam_user.web.name
}
