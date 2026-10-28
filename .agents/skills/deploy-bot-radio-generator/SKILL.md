---
name: deploy-bot-radio-generator
description: Redeploy The Bot Radio daily generator by building and pushing its Docker image to ECR, updating its Terraform-managed ECS Fargate task, and smoke-testing the deployed image. Use for generator releases and redeployments in the-bot-radio repository; website deployment and historical playlist backfills are separate workflows.
---

# Deploy The Bot Radio generator

Complete an authorized generator redeployment through remote smoke verification. Creating or
editing this skill does not authorize a deployment. A request to deploy authorizes the normal
image push, configuration edit, reviewed Terraform apply, and help task; do not ask again for
routine steps already authorized. Honor tool approval requirements and stop for changes outside
that scope. A request for instructions alone calls for a walkthrough.

## Establish the release

Use the current `the-bot-radio` checkout; its usual location is
`/Users/nevilgeorge/development/the-bot-radio`. Read its `AGENTS.md`, `deploy/generation.md`,
`deploy/release-generator.sh`, and relevant Terraform configuration before acting. Repository
instructions and current scripts take precedence over examples here. Use the AWS containers
skill when available for ECS/ECR details.

Run commands from the repository root with the existing AWS profile and Terraform backend.
Requirements are Terraform, AWS CLI v2, Docker with Buildx and AMD64 support, Git, Python 3,
and Bash. Use initialized state; if necessary initialize with the existing `backend.hcl`.
Never create a new backend or cloud foundation as part of a routine redeployment.

1. Check `git status --short --branch`, HEAD, and remotes. If the user requests the latest
   upstream version and the tree is clean, fast-forward the intended branch. On this repo,
   `git pull --ff-only origin main` works even when local main lacks upstream tracking.
   Do not reset changes, switch away from user work, or silently release a dirty checkout.
   The image includes current source files; the tag's commit identifies HEAD only.
2. Confirm the AWS account with `aws sts get-caller-identity`, Docker with `docker info`,
   and Terraform outputs for the region, ECR repository, cluster, task definition, and schedule.
   Inspect the actual schedule state and any running generator tasks. Do not stop an existing
   task or overlap a real generation for the same date. Never print secret values or full state.
3. Record the previous image digest, task-definition ARN, and configured/actual schedule state.
   Resolve mismatches before changing them. Inspect `generation_image_uri` and
   `generation_schedule_enabled` without dumping unrelated private configuration.
4. Run `uv run ruff check .`, `uv run ruff format --check .`, and `uv run pytest` unless already
   run successfully for this exact source tree in this session. Investigate failures before
   applying. Resolve relevant regressions. Missing local historical fixtures are test failures,
   not evidence of a broken container; establish and report the cause, and use the release
   smoke checks to verify the deployable artifact. Do not invent fixtures, silently skip
   failures, or claim the full suite passed. For release-script changes also run the
   shell checks documented in `deploy/generation.md`.

## Build and deploy

Keep an already disabled schedule disabled. If scheduling is enabled, disable it through a
reviewed Terraform plan/apply before rollout; remember its previous state and explain the pause
and conditions for restoring it before mutation. If this would skip an
imminent run, surface the timing decision before proceeding. Enabling a previously disabled
schedule requires an explicit user request; a redeploy alone does not authorize it.

Run:

```bash
./deploy/release-generator.sh
```

The script authenticates to ECR, builds `linux/amd64`, runs offline CLI/SDK smoke checks, pushes
a unique immutable tag, and prints a digest URI to stdout. Progress goes to stderr. It does
not apply Terraform or generate a playlist. Stop on failure; retain the successful digest
so a later Terraform failure does not cause an unnecessary rebuild. Do not expose credentials
or add ad hoc secrets to the build.

Edit only `generation_image_uri` in the existing untracked `terraform/terraform.tfvars` to the
printed repository digest URI, retaining `generation_schedule_enabled = false` during rollout.
Preserve other settings. Then:

```bash
terraform -chdir=terraform plan -out=generation.tfplan
```

Inspect the complete plan before applying. Expected changes concern the generator task-definition
revision, Scheduler target, and related output/permissions needed for that revision. Investigate
unexpected changes, especially Lightsail replacement, bucket changes, IAM expansion, networking,
secrets, or scheduling activation. Do not apply unrelated drift or hide it with `-target`.
If the plan is within the authorized release scope, apply the reviewed saved plan:

```bash
terraform -chdir=terraform apply generation.tfplan
```

This is a standalone ECS task, not an ECS service; do not use service force-deployment commands.
Updating the definition affects future launches and does not replace already running tasks.
Do not recreate or rotate existing Secrets Manager values during a code release.

## Verify and report

Run the deployed smoke test:

```bash
./deploy/generate-remote.py --help
```

This launches a real Fargate task with the deployed image and secret injection, incurs normal
Fargate charges, and makes no paid model calls or Spotify mutations. Require help output and
container exit code zero. Verify that the deployed task definition references the new digest
and that the schedule targets the intended revision. Use the wrapper's task ARN, ECS task
status, and matching CloudWatch stream to investigate failures; report them without secrets.
A wrapper monitoring failure can leave its task running. Inspect the printed task ARN before
launching another smoke task or declaring completion.

A real generation is a separate live action: it calls providers, creates a public Spotify
playlist, and replaces the date's daily record after validation. Run it only when explicitly
requested, using today's New York date by default (omit `--date`). An old `--date` mixes live
weather/transit/bike readings with date-derived or historical inputs and is not a faithful
historical replay. Never describe a backdated run as a harmless smoke test.

Keep a previously disabled schedule disabled after smoke verification. Before restoring an
enabled schedule or enabling one at the user's request, follow the repository's end-to-end
verification requirement; obtain authorization for any needed real generation. The configured
schedule is 06:00 America/New_York. Verify actual Scheduler state after any schedule apply.

On a failed apply or smoke test, inspect actual deployed state and keep scheduling disabled.
Do not loop on real generation, stop tasks automatically, or claim rollback occurred. If rollback
is requested, restore the recorded previous digest and review/apply its Terraform plan; verify
that ECS can launch that revision, then smoke-test it. Do not roll back secrets or Spotify data.

Report the source commit, image digest, new task-definition ARN, plan/apply outcome, smoke task
result, and actual schedule state. Distinguish deployment verification from a real successful
generation. Include any remaining test failure, deferred end-to-end check, or recovery work.
