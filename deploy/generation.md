# Stage the daily generator

## Current deployment

The production daily schedule was enabled on October 28, 2026 after successful end-to-end
runs. It launches at 06:00 America/New_York every day, following daylight saving time.
The operator accepted Scheduler's at-least-once delivery risk; durable duplicate-run
protection is deferred. Keep `generation_schedule_enabled = true` for normal operation.
The staging steps below describe initial setup, with scheduling disabled during rollout.

Commands run from the repository root, using the same AWS profile and Terraform backend
as hosting. Requirements: Terraform, AWS CLI v2, Docker with Buildx and AMD64 support,
Git, Python 3, and Bash. The generator uses Fargate On-Demand, 1 vCPU / 4 GiB RAM,
Linux X86_64, platform 1.4.0, and 20 GiB ephemeral disk. There is no ECS service.

## Rollout

1. With `generation_image_uri = null` and `generation_schedule_enabled = false`, run:

   ```bash
   terraform -chdir=terraform plan -out=generation.tfplan
   terraform -chdir=terraform apply generation.tfplan
   ```

   Review the saved plan first: existing Lightsail and S3 resources must have no changes.
   This creates ECR, a VPC with two public subnets and outbound-only task security group,
   ECS cluster, logs, roles, an encrypted dead-letter queue, and a secret resource.
   It creates no task definition or schedule until an image digest is supplied.

2. Read the secret ARN:

   ```bash
   terraform -chdir=terraform output -raw generation_secret_arn
   ```

   In the Secrets Manager console in `us-east-1`, open that secret and set its secret value
   as JSON with exactly these keys. Enter real values only in the console; do not put them
   in repository files, Terraform variables, state, shell arguments, or this example:

   ```json
   {
     "ANTHROPIC_API_KEY": "",
     "SPOTIFY_CLIENT_ID": "",
     "SPOTIFY_CLIENT_SECRET": "",
     "SPOTIFY_REFRESH_TOKEN": "",
     "NYC_OPEN_DATA_APP_TOKEN": ""
   }
   ```

   The four API/Spotify values are required. The NYC token may remain empty. Ensure the
   version has the `AWSCURRENT` staging label. Terraform never reads or writes its value.
   Each new ECS task retrieves the five individual JSON keys with the execution role.

3. Build and release:

   ```bash
   ./deploy/release-generator.sh
   ```

   The script reads Terraform outputs, authenticates Docker to ECR, builds `linux/amd64`,
   runs offline CLI and SDK executable smoke checks, and pushes a unique immutable tag
   containing the Git commit and a UUID. Its stdout is the digest URI; progress goes to
   stderr. It does not apply Terraform, alter scheduling, or run generation. A failed
   command stops release. Use a clean working tree when recording releases: the commit
   portion identifies HEAD, while the build includes current source files.

4. Set `generation_image_uri` in your untracked `terraform/terraform.tfvars` to the printed
   repository digest URI, retaining `generation_schedule_enabled = false`. Plan and apply
   again to register the task definition and **disabled** daily schedule.

5. Run the manual help task below. Verify exit code zero and CloudWatch output. This
   exercises image pull, secret injection and non-root application startup without paid
   API calls. The image release checks UID 10001, writable home/data/workspace, bundled
   Linux Claude CLI startup, the required packaged inputs, and the absence of credentials.

6. Keep scheduling disabled until an end-to-end generation check passes. The image includes
   `personas.json` and `shared_catalog.jsonl` through the installed
   `the_bot_radio.resources` package. Rebuild and release when these resources change.
   `/work/data` starts empty and is reserved for writable runtime storage.
   Daily records and run artifacts use the configured S3 location; run workspaces remain
   ephemeral. Existing daily records, previous runs, and credentials are excluded from images.

## One-off remote generator

Use the same arguments as `the-bot-radio-generate`:

```bash
./deploy/generate-remote.py --help
./deploy/generate-remote.py --date 2026-10-28 --verbose --budget-usd 5
```

Requirements: Python 3.9+ (standard library only), Terraform initialized with the existing
backend, and AWS CLI v2 authenticated with permission to run this task, pass its execution
and task roles, describe/stop tasks, and read its CloudWatch log stream. `AWS_PROFILE` selects
the operator profile as usual. Run the script from any directory.

The script reads the deployed Terraform outputs and starts one Fargate task using the
registered image, secrets, sizing and network configuration. It forwards arguments verbatim,
including `--help`, `--date`, `--data-dir`, `--model`, `--max-turns`, `--budget-usd`,
`--deadline-seconds`, and `--verbose`. No arguments uses the generator's usual defaults,
including non-verbose output. Help and argument validation run in the deployed image, so
they reflect the remote CLI version. `--help` is the safe smoke check: it starts a real
Fargate container and makes no paid API calls.

The task ARN goes to stderr. CloudWatch output streams to stdout (container stdout and
stderr are merged by the log driver). The script waits for completion and returns the
container exit code, or 1 for launch/monitoring failures. Ctrl+C requests that ECS stop the
remote task and returns 130. If interrupted before the launch response arrives, inspect ECS
for a task; the script may not yet have received its ARN. A monitoring failure reports the
ARN and leaves the job running for inspection. Logs can lag by several seconds.

Paths such as `--data-dir` refer to the **container filesystem**. Local files and environment
variables such as `RUN_DATE` are not uploaded; pass `--date` explicitly. The deployed task
uses the managed data bucket's `production` prefix for daily records and run artifacts.
Personas and the catalog come from the installed package; input descriptions are not used
by the generator. Local run workspaces use temporary container storage. This script does not change the schedule.

To inspect a task independently, use its printed ARN and Terraform's
`generation_cluster_arn` and `generation_log_group_name` outputs:

```bash
aws ecs describe-tasks --region us-east-1 --cluster CLUSTER-ARN --tasks TASK-ARN \
  --query 'tasks[].{stopCode:stopCode,reason:stoppedReason,containers:containers[].{name:name,exitCode:exitCode,reason:reason}}'
aws logs tail LOG-GROUP-NAME --region us-east-1 --since 30m
```

A successful smoke has container exit code `0`, help text in its log stream, and no
initialization failure. To test the wrapper without AWS:

```bash
uv run pytest tests/test_generate_remote.py -q
```

## Spotify publication

Each real generation publishes a fresh Spotify playlist automatically. The daily title and Spotify
name use `YYYY-MM-DD — <generated title>`. The playlist is created publicly, populated with all
ten ordered tracks, and read back to verify its name, public visibility, and ordered contents.
All storage environments create public playlists, including local files and development;
delete test playlists manually when finished. Completed records are published and visible on the
website. S3 website snapshots refresh 60 seconds
after each refresh attempt, so a successful generation may take several minutes to appear.

Failures before daily commit preserve the previous daily bytes. A created playlist is retained
on failure or cancellation; inspect the known ID and URL in `diagnostics.json`, `publication.json`,
and the CLI recovery notes before generating again. There is no automatic deletion, resumption,
or rollback of Spotify playlists. Ambiguous Spotify mutations are not retried; a lost create
response can leave an unidentified orphan. Artifact recovery only uploads evidence and never
calls Spotify or republishes the daily record. Existing pending files are not backfilled.

## Schedule, failures, and rollback

The staged schedule is `cron(0 6 * * ? *)`, timezone `America/New_York`, with no flexible
window, one task, two delivery retries and a one-hour maximum event age. Scheduler retries
cover launch delivery; they do not detect or retry a container that launches and fails.
There is no automated alert destination in this stage.

Use `generation_schedule_name` and its group (the same name) in the Scheduler console to
confirm `ENABLED` for normal operation or `DISABLED` during rollout. Foundation-only deployments have no schedule. Inspect the queue with:

```bash
terraform -chdir=terraform output -raw generation_dead_letter_queue_url
aws sqs get-queue-attributes --region us-east-1 --queue-url QUEUE-URL \
  --attribute-names ApproximateNumberOfMessages ApproximateNumberOfMessagesNotVisible
aws sqs receive-message --region us-east-1 --queue-url QUEUE-URL \
  --max-number-of-messages 10 --visibility-timeout 30 --message-system-attribute-names All
```

Receiving temporarily hides messages; do not delete them until investigated. Messages expire
in 14 days. CloudWatch stdout/stderr logs expire in 30 days. For task failures inspect
`stoppedReason`, `stopCode`, container `reason` and `exitCode`, then the matching log stream
`generator/generator/TASK-ID`. Missing logs often indicate image pull or secret injection
failed before startup; verify the digest, `AWSCURRENT` keys, execution permissions, and
public-IP/subnet routes. No inbound security rules are needed.

Application failures include `error_type`, `error_message`, `error_traceback`, and `phase`
in `diagnostics.json`. Tracebacks include exception causes without local variables;
environment credential values are redacted. The CLI also prints the redacted failure reason.
A `preparation` failure reporting `shared catalog is missing or empty` means the image
is missing its packaged resources; rebuild and deploy the corrected image before retrying.

Roll back by restoring the previous `generation_image_uri` digest and applying Terraform,
keeping scheduling disabled. Tagged ECR releases are retained; only untagged images older
than 14 days expire. A task-definition change affects future launches and does not stop an
already running standalone task. Secret rollback/rotation is separate and uses Secrets Manager.

## IAM review and local validation

The execution role can authenticate to ECR (the API requires `Resource = "*"`), pull only
this repository, write only this log group, and retrieve only this secret. The application
task role can read/write `production/days/*`, write `production/runs/*`, and list the data
bucket, matching the deployed policy. It has no delete permission. Scheduler can run only this task family in this cluster,
pass only the two ECS roles to ECS tasks, and send only to its dead-letter queue.

Trust restrictions follow AWS documentation: [Scheduler uses a schedule-group source ARN](https://docs.aws.amazon.com/scheduler/latest/UserGuide/cross-service-confused-deputy-prevention.html),
and [ECS supports an account/region wildcard source ARN](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/task-iam-roles.html).
Both also restrict the source account. [Secret key injection](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/secrets-envvar-secrets-manager.html)
uses `key:AWSCURRENT:` and Fargate 1.4.0.

```bash
terraform fmt -check -recursive terraform
terraform -chdir=terraform validate
terraform -chdir=terraform test
bash -n deploy/release-generator.sh
uv tool run --from shellcheck-py shellcheck deploy/release-generator.sh
uv run pytest tests/test_release_generator.py -q
```

For a permissions cross-check, render the saved plan to an owner-readable temporary file
and run IAM Policy Autopilot (substitute your account ID):

```bash
umask 077
terraform -chdir=terraform show -json generation.tfplan > /tmp/bot-radio-generation-plan.json
uvx iam-policy-autopilot@latest generate-policies /tmp/bot-radio-generation-plan.json \
  --region us-east-1 --account YOUR-ACCOUNT-ID --pretty
```

Autopilot reports deployment permissions inferred from resource changes; review the explicit
runtime roles separately against the AWS documentation linked above. Never attach its
output automatically. Delete the temporary plan JSON after review; saved plans and state
can include sensitive infrastructure attributes.
