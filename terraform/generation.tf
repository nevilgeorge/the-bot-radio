data "aws_caller_identity" "generation" {}
data "aws_partition" "generation" {}

locals {
  generation_region = "us-east-1"
  generation_name   = "${var.name_prefix}-generation"
  application_s3_environment = {
    AWS_DEFAULT_REGION  = local.generation_region
    BOT_RADIO_S3_BUCKET = aws_s3_bucket.site["data"].id
    BOT_RADIO_S3_PREFIX = "production"
  }
  generation_secret_keys = [
    "ANTHROPIC_API_KEY", "SPOTIFY_CLIENT_ID", "SPOTIFY_CLIENT_SECRET",
    "SPOTIFY_REFRESH_TOKEN", "NYC_OPEN_DATA_APP_TOKEN"
  ]
  generation_task_family_arn = "arn:${data.aws_partition.generation.partition}:ecs:${local.generation_region}:${data.aws_caller_identity.generation.account_id}:task-definition/${local.generation_name}:*"
  generation_ecs_trust = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = {
        StringEquals = { "aws:SourceAccount" = data.aws_caller_identity.generation.account_id }
        # ECS does not support restricting task-role trust to a specific cluster.
        ArnLike = { "aws:SourceArn" = "arn:${data.aws_partition.generation.partition}:ecs:${local.generation_region}:${data.aws_caller_identity.generation.account_id}:*" }
      }
    }]
  })
}

resource "aws_ecr_repository" "generation" {
  name                 = local.generation_name
  image_tag_mutability = "IMMUTABLE"
  force_delete         = false
  encryption_configuration {
    encryption_type = "AES256"
  }
  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecr_lifecycle_policy" "generation" {
  repository = aws_ecr_repository.generation.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Expire only untagged images after 14 days; retain tagged releases for rollback"
      selection = {
        tagStatus   = "untagged"
        countType   = "sinceImagePushed"
        countUnit   = "days"
        countNumber = 14
      }
      action = { type = "expire" }
    }]
  })
}

resource "aws_ecs_cluster" "generation" {
  name = local.generation_name
}

resource "aws_vpc" "generation" {
  cidr_block           = "10.42.0.0/16"
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags                 = { Name = local.generation_name }
}

resource "aws_subnet" "generation" {
  for_each = {
    us-east-1a = "10.42.0.0/24"
    us-east-1b = "10.42.1.0/24"
  }
  vpc_id            = aws_vpc.generation.id
  availability_zone = each.key
  cidr_block        = each.value
  tags              = { Name = "${local.generation_name}-${each.key}" }
}

resource "aws_internet_gateway" "generation" {
  vpc_id = aws_vpc.generation.id
}

resource "aws_route_table" "generation" {
  vpc_id = aws_vpc.generation.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.generation.id
  }
}

resource "aws_route_table_association" "generation" {
  for_each       = aws_subnet.generation
  subnet_id      = each.value.id
  route_table_id = aws_route_table.generation.id
}

resource "aws_security_group" "generation" {
  name        = local.generation_name
  description = "Generator outbound API access; no inbound connections"
  vpc_id      = aws_vpc.generation.id
  ingress     = []
  egress {
    description = "HTTPS APIs, DNS and container runtime outbound access"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_cloudwatch_log_group" "generation" {
  name              = "/ecs/${local.generation_name}"
  retention_in_days = 30
}

# Only the secret metadata belongs in Terraform. Populate AWSCURRENT in the console.
resource "aws_secretsmanager_secret" "generation" {
  name                    = "${var.name_prefix}/generation"
  description             = "Daily generator API credentials (JSON keys injected by ECS)"
  recovery_window_in_days = 30
}

resource "aws_iam_role" "generation_execution" {
  name               = "${var.name_prefix}-gen-execution"
  assume_role_policy = local.generation_ecs_trust
}

resource "aws_iam_role_policy" "generation_execution" {
  name = "image-logs-secrets"
  role = aws_iam_role.generation_execution.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "EcrAuthentication"
        Effect   = "Allow"
        Action   = ["ecr:GetAuthorizationToken"]
        Resource = "*"
      },
      {
        Sid      = "PullGeneratorImage"
        Effect   = "Allow"
        Action   = ["ecr:BatchCheckLayerAvailability", "ecr:GetDownloadUrlForLayer", "ecr:BatchGetImage"]
        Resource = aws_ecr_repository.generation.arn
      },
      {
        Sid      = "GeneratorLogs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.generation.arn}:*"
      },
      {
        Sid      = "GeneratorCredentials"
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = aws_secretsmanager_secret.generation.arn
      }
    ]
  })
}

# Preserve the deployed generator's daily-record and diagnostic-artifact access.
resource "aws_iam_role" "generation_task" {
  name               = "${var.name_prefix}-gen-task"
  assume_role_policy = local.generation_ecs_trust
}

resource "aws_iam_role_policy" "generation_task" {
  name = "application-data"
  role = aws_iam_role.generation_task.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "DailyRecords"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject"]
        Resource = "${aws_s3_bucket.site["data"].arn}/production/days/*"
      },
      {
        Sid      = "RunRecords"
        Effect   = "Allow"
        Action   = ["s3:PutObject"]
        Resource = "${aws_s3_bucket.site["data"].arn}/production/runs/*"
      },
      {
        Sid      = "MissingDailyRecords"
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = aws_s3_bucket.site["data"].arn
      }
    ]
  })
}

resource "aws_sqs_queue" "generation_dead_letter" {
  name                      = "${local.generation_name}-dead-letter"
  message_retention_seconds = 1209600
  sqs_managed_sse_enabled   = true
}

resource "aws_scheduler_schedule_group" "generation" {
  name = local.generation_name
}

resource "aws_iam_role" "generation_scheduler" {
  name = "${var.name_prefix}-gen-scheduler"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "scheduler.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = {
        StringEquals = {
          "aws:SourceAccount" = data.aws_caller_identity.generation.account_id
          # Scheduler supplies the schedule GROUP ARN, not the schedule ARN.
          "aws:SourceArn" = aws_scheduler_schedule_group.generation.arn
        }
      }
    }]
  })
}

resource "aws_iam_role_policy" "generation_scheduler" {
  name = "launch-generator"
  role = aws_iam_role.generation_scheduler.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "RunGenerator"
        Effect    = "Allow"
        Action    = ["ecs:RunTask"]
        Resource  = local.generation_task_family_arn
        Condition = { ArnEquals = { "ecs:cluster" = aws_ecs_cluster.generation.arn } }
      },
      {
        Sid       = "PassGeneratorRoles"
        Effect    = "Allow"
        Action    = ["iam:PassRole"]
        Resource  = [aws_iam_role.generation_execution.arn, aws_iam_role.generation_task.arn]
        Condition = { StringEquals = { "iam:PassedToService" = "ecs-tasks.amazonaws.com" } }
      },
      {
        Sid      = "DeadLetterDelivery"
        Effect   = "Allow"
        Action   = ["sqs:SendMessage"]
        Resource = aws_sqs_queue.generation_dead_letter.arn
      }
    ]
  })
}

resource "aws_ecs_task_definition" "generation" {
  count                    = var.generation_image_uri == null ? 0 : 1
  family                   = local.generation_name
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = "1024"
  memory                   = "4096"
  execution_role_arn       = aws_iam_role.generation_execution.arn
  task_role_arn            = aws_iam_role.generation_task.arn
  runtime_platform {
    cpu_architecture        = "X86_64"
    operating_system_family = "LINUX"
  }
  # Omit ephemeral_storage to use Fargate's default 20 GiB.
  container_definitions = jsonencode([{
    name            = "generator"
    image           = var.generation_image_uri
    essential       = true
    cpu             = 1024
    memory          = 4096
    user            = "10001"
    stopTimeout     = 120
    linuxParameters = { initProcessEnabled = true }
    environment = [for name, value in merge(local.application_s3_environment, {
      BOT_RADIO_DATA_DIR = "/work/data"
      TZ                 = "America/New_York"
      PYTHONUNBUFFERED   = "1"
      }) : {
      name = name, value = value
    }]
    secrets = [for key in local.generation_secret_keys : {
      name      = key
      valueFrom = "${aws_secretsmanager_secret.generation.arn}:${key}:AWSCURRENT:"
    }]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.generation.name
        awslogs-region        = local.generation_region
        awslogs-stream-prefix = "generator"
      }
    }
  }])
  depends_on = [aws_iam_role_policy.generation_execution, aws_iam_role_policy.generation_task]
}

resource "aws_scheduler_schedule" "generation" {
  count                        = var.generation_image_uri == null ? 0 : 1
  name                         = local.generation_name
  group_name                   = aws_scheduler_schedule_group.generation.name
  state                        = var.generation_schedule_enabled ? "ENABLED" : "DISABLED"
  schedule_expression          = "cron(0 6 * * ? *)"
  schedule_expression_timezone = "America/New_York"
  flexible_time_window {
    mode = "OFF"
  }
  target {
    arn      = aws_ecs_cluster.generation.arn
    role_arn = aws_iam_role.generation_scheduler.arn
    ecs_parameters {
      task_definition_arn = aws_ecs_task_definition.generation[0].arn
      launch_type         = "FARGATE"
      platform_version    = "1.4.0"
      task_count          = 1
      network_configuration {
        subnets          = [for subnet in aws_subnet.generation : subnet.id]
        security_groups  = [aws_security_group.generation.id]
        assign_public_ip = true
      }
    }
    retry_policy {
      maximum_retry_attempts       = 2
      maximum_event_age_in_seconds = 3600
    }
    dead_letter_config {
      arn = aws_sqs_queue.generation_dead_letter.arn
    }
  }
  depends_on = [aws_iam_role_policy.generation_scheduler, aws_route_table_association.generation]
}
