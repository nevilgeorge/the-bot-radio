mock_provider "aws" {
  mock_resource "aws_s3_bucket" {
    defaults = {
      id  = "the-bot-radio-test-data"
      arn = "arn:aws:s3:::the-bot-radio-test-data"
    }
  }
  mock_data "aws_caller_identity" {
    defaults = { account_id = "123456789012" }
  }
  mock_data "aws_partition" {
    defaults = { partition = "aws" }
  }
  mock_resource "aws_ecr_repository" {
    defaults = {
      repository_url = "123456789012.dkr.ecr.us-east-1.amazonaws.com/the-bot-radio-test-generation"
      arn            = "arn:aws:ecr:us-east-1:123456789012:repository/the-bot-radio-test-generation"
    }
  }
  mock_resource "aws_cloudwatch_log_group" {
    defaults = { arn = "arn:aws:logs:us-east-1:123456789012:log-group:/ecs/the-bot-radio-test-generation" }
  }
  mock_resource "aws_secretsmanager_secret" {
    defaults = { arn = "arn:aws:secretsmanager:us-east-1:123456789012:secret:the-bot-radio-test/generation-AbCdEf" }
  }
  mock_resource "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::123456789012:role/mock-generation" }
  }
  mock_resource "aws_ecs_task_definition" {
    defaults = { arn = "arn:aws:ecs:us-east-1:123456789012:task-definition/the-bot-radio-test-generation:1" }
  }
  mock_resource "aws_sqs_queue" {
    defaults = { arn = "arn:aws:sqs:us-east-1:123456789012:the-bot-radio-test-generation-dead-letter" }
  }
  mock_resource "aws_ecs_cluster" {
    defaults = { arn = "arn:aws:ecs:us-east-1:123456789012:cluster/the-bot-radio-test-generation" }
  }
  mock_resource "aws_scheduler_schedule_group" {
    defaults = { arn = "arn:aws:scheduler:us-east-1:123456789012:schedule-group/the-bot-radio-test-generation" }
  }
}

variables {
  name_prefix       = "the-bot-radio-test"
  ssh_public_key    = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITestKey"
  ssh_allowed_cidrs = ["198.51.100.7/32"]
}

run "foundation_only" {
  command = apply
  assert {
    condition     = length(aws_ecs_task_definition.generation) == 0 && length(aws_scheduler_schedule.generation) == 0 && output.generation_task_definition_arn == null && output.generation_schedule_name == null
    error_message = "Foundation must deploy without an image or runnable schedule."
  }
  assert {
    condition     = aws_ecr_repository.generation.image_tag_mutability == "IMMUTABLE" && !aws_ecr_repository.generation.force_delete && aws_ecr_repository.generation.encryption_configuration[0].encryption_type == "AES256"
    error_message = "Release tags must be immutable and repository deletion safe."
  }
  assert {
    condition     = length(jsondecode(aws_ecr_lifecycle_policy.generation.policy).rules) == 1 && jsondecode(aws_ecr_lifecycle_policy.generation.policy).rules[0].selection.tagStatus == "untagged" && jsondecode(aws_ecr_lifecycle_policy.generation.policy).rules[0].selection.countNumber == 14 && jsondecode(aws_ecr_lifecycle_policy.generation.policy).rules[0].selection.countType == "sinceImagePushed"
    error_message = "Retain every tagged rollback image and expire only untagged images after 14 days."
  }
  assert {
    condition     = aws_vpc.generation.cidr_block == "10.42.0.0/16" && aws_vpc.generation.enable_dns_support && aws_vpc.generation.enable_dns_hostnames && length(aws_subnet.generation) == 2 && aws_subnet.generation["us-east-1a"].cidr_block == "10.42.0.0/24" && aws_subnet.generation["us-east-1b"].cidr_block == "10.42.1.0/24" && alltrue([for zone, subnet in aws_subnet.generation : subnet.availability_zone == zone && subnet.vpc_id == aws_vpc.generation.id])
    error_message = "Use a dedicated VPC and the two specified public subnets."
  }
  assert {
    condition     = length(aws_security_group.generation.ingress) == 0 && one(aws_security_group.generation.egress).protocol == "-1" && contains(one(aws_security_group.generation.egress).cidr_blocks, "0.0.0.0/0") && one(aws_route_table.generation.route).gateway_id == aws_internet_gateway.generation.id && one(aws_route_table.generation.route).cidr_block == "0.0.0.0/0" && length(aws_route_table_association.generation) == 2 && alltrue([for association in aws_route_table_association.generation : association.route_table_id == aws_route_table.generation.id])
    error_message = "Provide internet egress with no inbound security group access."
  }
  assert {
    condition     = aws_secretsmanager_secret.generation.name == "the-bot-radio-test/generation" && aws_secretsmanager_secret.generation.recovery_window_in_days == 30 && aws_cloudwatch_log_group.generation.retention_in_days == 30 && aws_sqs_queue.generation_dead_letter.sqs_managed_sse_enabled && aws_sqs_queue.generation_dead_letter.message_retention_seconds == 1209600
    error_message = "Stage recoverable secret metadata, 30-day logs and encrypted 14-day dead letters."
  }
  assert {
    condition     = jsondecode(aws_iam_role.generation_task.assume_role_policy).Statement[0].Principal.Service == "ecs-tasks.amazonaws.com" && jsondecode(aws_iam_role.generation_task.assume_role_policy).Statement[0].Condition.StringEquals["aws:SourceAccount"] == "123456789012" && jsondecode(aws_iam_role.generation_task.assume_role_policy).Statement[0].Condition.ArnLike["aws:SourceArn"] == "arn:aws:ecs:us-east-1:123456789012:*" && aws_iam_role.generation_task.assume_role_policy == aws_iam_role.generation_execution.assume_role_policy
    error_message = "Both ECS roles must trust only same-account ECS with supported source restrictions."
  }
  assert {
    condition = jsondecode(aws_iam_role_policy.generation_task.policy) == {
      Version = "2012-10-17"
      Statement = [
        {
          Sid      = "DailyRecords"
          Effect   = "Allow"
          Action   = ["s3:GetObject", "s3:PutObject"]
          Resource = "arn:aws:s3:::the-bot-radio-test-data/production/days/*"
        },
        {
          Sid      = "RunRecords"
          Effect   = "Allow"
          Action   = ["s3:PutObject"]
          Resource = "arn:aws:s3:::the-bot-radio-test-data/production/runs/*"
        },
        {
          Sid      = "MissingDailyRecords"
          Effect   = "Allow"
          Action   = ["s3:ListBucket"]
          Resource = "arn:aws:s3:::the-bot-radio-test-data"
        }
      ]
    } && aws_iam_role_policy.generation_task.role == aws_iam_role.generation_task.id
    error_message = "Retain the deployed generator's daily read/write, run write and missing-record listing permissions."
  }
  assert {
    condition     = jsondecode(aws_iam_role.generation_scheduler.assume_role_policy).Statement[0].Condition.StringEquals["aws:SourceArn"] == aws_scheduler_schedule_group.generation.arn && jsondecode(aws_iam_role.generation_scheduler.assume_role_policy).Statement[0].Condition.StringEquals["aws:SourceAccount"] == "123456789012"
    error_message = "Scheduler trust must restrict the schedule group and source account."
  }
  assert {
    condition = length(jsondecode(aws_iam_role_policy.generation_execution.policy).Statement) == 4 && alltrue([
      for statement in jsondecode(aws_iam_role_policy.generation_execution.policy).Statement :
      statement.Sid == "EcrAuthentication" ? statement.Resource == "*" && toset(statement.Action) == toset(["ecr:GetAuthorizationToken"]) :
      statement.Sid == "PullGeneratorImage" ? statement.Resource == aws_ecr_repository.generation.arn && toset(statement.Action) == toset(["ecr:BatchCheckLayerAvailability", "ecr:GetDownloadUrlForLayer", "ecr:BatchGetImage"]) :
      statement.Sid == "GeneratorLogs" ? statement.Resource == "${aws_cloudwatch_log_group.generation.arn}:*" && toset(statement.Action) == toset(["logs:CreateLogStream", "logs:PutLogEvents"]) :
      statement.Sid == "GeneratorCredentials" && statement.Resource == aws_secretsmanager_secret.generation.arn && toset(statement.Action) == toset(["secretsmanager:GetSecretValue"])
    ])
    error_message = "Execution permissions must be limited to image authentication/pull, this log group and this secret."
  }
  assert {
    condition     = length(jsondecode(aws_iam_role_policy.generation_scheduler.policy).Statement) == 3 && jsondecode(aws_iam_role_policy.generation_scheduler.policy).Statement[0].Resource == "arn:aws:ecs:us-east-1:123456789012:task-definition/the-bot-radio-test-generation:*" && jsondecode(aws_iam_role_policy.generation_scheduler.policy).Statement[0].Action == ["ecs:RunTask"] && jsondecode(aws_iam_role_policy.generation_scheduler.policy).Statement[0].Condition.ArnEquals["ecs:cluster"] == aws_ecs_cluster.generation.arn && toset(jsondecode(aws_iam_role_policy.generation_scheduler.policy).Statement[1].Resource) == toset([aws_iam_role.generation_execution.arn, aws_iam_role.generation_task.arn]) && jsondecode(aws_iam_role_policy.generation_scheduler.policy).Statement[1].Action == ["iam:PassRole"] && jsondecode(aws_iam_role_policy.generation_scheduler.policy).Statement[1].Condition.StringEquals["iam:PassedToService"] == "ecs-tasks.amazonaws.com" && jsondecode(aws_iam_role_policy.generation_scheduler.policy).Statement[2].Resource == aws_sqs_queue.generation_dead_letter.arn && jsondecode(aws_iam_role_policy.generation_scheduler.policy).Statement[2].Action == ["sqs:SendMessage"]
    error_message = "Scheduler must launch only this task family/cluster, pass only two ECS roles and send only to its dead-letter queue."
  }
}

run "digest_stages_disabled_schedule" {
  command = apply
  variables {
    generation_image_uri = "123456789012.dkr.ecr.us-east-1.amazonaws.com/the-bot-radio-test-generation@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  }
  assert {
    condition     = length(aws_ecs_task_definition.generation) == 1 && aws_ecs_task_definition.generation[0].cpu == "1024" && aws_ecs_task_definition.generation[0].memory == "4096" && aws_ecs_task_definition.generation[0].network_mode == "awsvpc" && aws_ecs_task_definition.generation[0].requires_compatibilities == toset(["FARGATE"]) && aws_ecs_task_definition.generation[0].runtime_platform[0].cpu_architecture == "X86_64" && aws_ecs_task_definition.generation[0].runtime_platform[0].operating_system_family == "LINUX" && length(aws_ecs_task_definition.generation[0].ephemeral_storage) == 0
    error_message = "Use AMD64 Linux Fargate with 1 vCPU, 4 GB RAM and default ephemeral storage."
  }
  assert {
    condition     = length(jsondecode(aws_ecs_task_definition.generation[0].container_definitions)) == 1 && one(jsondecode(aws_ecs_task_definition.generation[0].container_definitions)).essential && one(jsondecode(aws_ecs_task_definition.generation[0].container_definitions)).image == var.generation_image_uri && one(jsondecode(aws_ecs_task_definition.generation[0].container_definitions)).user == "10001" && one(jsondecode(aws_ecs_task_definition.generation[0].container_definitions)).linuxParameters.initProcessEnabled && one(jsondecode(aws_ecs_task_definition.generation[0].container_definitions)).stopTimeout == 120 && one(jsondecode(aws_ecs_task_definition.generation[0].container_definitions)).logConfiguration.options["awslogs-group"] == aws_cloudwatch_log_group.generation.name
    error_message = "A single non-root essential generator must receive the digest, init, logging and stop timeout."
  }
  assert {
    condition = {
      for variable in one(jsondecode(aws_ecs_task_definition.generation[0].container_definitions)).environment :
      variable.name => variable.value
      } == {
      AWS_DEFAULT_REGION  = "us-east-1"
      BOT_RADIO_DATA_DIR  = "/work/data"
      BOT_RADIO_S3_BUCKET = "the-bot-radio-test-data"
      BOT_RADIO_S3_PREFIX = "production"
      PYTHONUNBUFFERED    = "1"
      TZ                  = "America/New_York"
    }
    error_message = "Generator tasks must keep the deployed production S3 location alongside local metadata and runtime settings."
  }
  assert {
    condition     = toset([for secret in one(jsondecode(aws_ecs_task_definition.generation[0].container_definitions)).secrets : secret.name]) == toset(["ANTHROPIC_API_KEY", "SPOTIFY_CLIENT_ID", "SPOTIFY_CLIENT_SECRET", "SPOTIFY_REFRESH_TOKEN", "NYC_OPEN_DATA_APP_TOKEN"]) && alltrue([for secret in one(jsondecode(aws_ecs_task_definition.generation[0].container_definitions)).secrets : secret.valueFrom == "${aws_secretsmanager_secret.generation.arn}:${secret.name}:AWSCURRENT:"])
    error_message = "Inject exactly the five individual secret JSON keys at AWSCURRENT."
  }
  assert {
    condition     = aws_scheduler_schedule.generation[0].state == "DISABLED" && aws_scheduler_schedule.generation[0].schedule_expression == "cron(0 6 * * ? *)" && aws_scheduler_schedule.generation[0].schedule_expression_timezone == "America/New_York" && aws_scheduler_schedule.generation[0].flexible_time_window[0].mode == "OFF" && aws_scheduler_schedule.generation[0].target[0].ecs_parameters[0].launch_type == "FARGATE" && aws_scheduler_schedule.generation[0].target[0].ecs_parameters[0].platform_version == "1.4.0" && aws_scheduler_schedule.generation[0].target[0].ecs_parameters[0].task_count == 1 && aws_scheduler_schedule.generation[0].target[0].ecs_parameters[0].network_configuration[0].assign_public_ip && toset(aws_scheduler_schedule.generation[0].target[0].ecs_parameters[0].network_configuration[0].subnets) == toset(output.generation_subnet_ids) && aws_scheduler_schedule.generation[0].target[0].ecs_parameters[0].network_configuration[0].security_groups == toset([output.generation_security_group_id])
    error_message = "Stage one On-Demand task at 06:00 New York in the public subnets, disabled by default."
  }
  assert {
    condition     = aws_scheduler_schedule.generation[0].target[0].retry_policy[0].maximum_retry_attempts == 2 && aws_scheduler_schedule.generation[0].target[0].retry_policy[0].maximum_event_age_in_seconds == 3600 && aws_scheduler_schedule.generation[0].target[0].dead_letter_config[0].arn == aws_sqs_queue.generation_dead_letter.arn
    error_message = "Launch delivery must have two retries, a one-hour age and the encrypted dead-letter queue."
  }
}

run "explicit_enable" {
  command = apply
  variables {
    generation_image_uri        = "123456789012.dkr.ecr.us-east-1.amazonaws.com/the-bot-radio-test-generation@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    generation_schedule_enabled = true
  }
  assert {
    condition     = aws_scheduler_schedule.generation[0].state == "ENABLED"
    error_message = "Explicit enablement with an image must be supported."
  }
}

run "reject_enable_without_image" {
  command = plan
  variables {
    generation_schedule_enabled = true
  }
  expect_failures = [var.generation_schedule_enabled]
}

run "reject_mutable_tag" {
  command = plan
  variables {
    generation_image_uri = "123456789012.dkr.ecr.us-east-1.amazonaws.com/the-bot-radio-test-generation:latest"
  }
  expect_failures = [var.generation_image_uri]
}

run "reject_other_repository" {
  command = plan
  variables {
    generation_image_uri = "123456789012.dkr.ecr.us-east-1.amazonaws.com/other@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  }
  expect_failures = [var.generation_image_uri]
}
