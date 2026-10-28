mock_provider "aws" {
  mock_resource "aws_s3_bucket" {
    defaults = {
      arn = "arn:aws:s3:::the-bot-radio-staging-data"
    }
  }
}

variables {
  name_prefix                 = "the-bot-radio-staging"
  ssh_public_key              = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITestKey"
  ssh_allowed_cidrs           = ["198.51.100.7/32"]
  generation_image_uri        = null
  generation_schedule_enabled = false
}

run "website_daily_reads" {
  command = apply

  assert {
    condition     = aws_iam_user.web.name == "the-bot-radio-staging-web" && output.web_iam_user_name == aws_iam_user.web.name && aws_iam_user_policy.web.user == aws_iam_user.web.name && !aws_iam_user.web.force_destroy
    error_message = "Expose a dedicated website IAM user without forced credential deletion."
  }
  assert {
    condition = jsondecode(aws_iam_user_policy.web.policy) == {
      Version = "2012-10-17"
      Statement = [
        {
          Sid       = "ListDailyRecords"
          Effect    = "Allow"
          Action    = ["s3:ListBucket"]
          Resource  = "arn:aws:s3:::the-bot-radio-staging-data"
          Condition = { StringEquals = { "s3:prefix" = "production/days/" } }
        },
        {
          Sid      = "ReadDailyRecords"
          Effect   = "Allow"
          Action   = ["s3:GetObject"]
          Resource = "arn:aws:s3:::the-bot-radio-staging-data/production/days/*"
        }
      ]
    }
    error_message = "Grant only daily reads and listing with the exact production/days/ prefix."
  }
}
