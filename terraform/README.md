# Lightsail hosting

Terraform manages the Lightsail instance, SSH public key, static IPv4, firewall, and retained
private S3 data bucket in `us-east-1`. The instance uses Ubuntu 24.04 and defaults to a 2 GB
`small_3_0` bundle. Caddy manages HTTPS; systemd runs FastAPI through Uvicorn on localhost.

For code changes, use [the deployment script](../deploy/README.md). Terraform is only needed
when infrastructure or SSH access settings change. Commands below run from the repository root.

## Update existing infrastructure

Use your existing AWS credentials and backend configuration; set `AWS_PROFILE` if needed.

```bash
terraform -chdir=terraform init -backend-config=backend.hcl
terraform -chdir=terraform plan -out=hosting.tfplan
terraform -chdir=terraform apply hosting.tfplan
```

Review the plan before applying. Keep `name_prefix` unchanged: it also determines the retained
data bucket name, whose `prevent_destroy` protection blocks replacement.

The website S3 rollout adds only `aws_iam_user.web` and `aws_iam_user_policy.web` with a
`web_iam_user_name` output. Inspect the complete plan for unrelated drift before applying;
do not accept Lightsail replacement, ECS changes, or bucket-policy changes as part of this
credential rollout. Confirm the selected AWS account owns the bucket before applying, using
`aws s3api head-bucket --bucket the-bot-radio-staging-data --expected-bucket-owner ACCOUNT-ID`.

Instance replacement loses its local data. Back up `/var/lib/the-bot-radio/data` before
approving replacement, then restore or seed the new server. Terraform recreates the static IP
attachment and firewall when replacing the instance. The static IP remains the same if its
resource is retained; the SSH host key changes and must be verified again.

## Set up a new installation

Requirements: Terraform >=1.10 and <2, AWS credentials permitted to manage Lightsail, S3, IAM,
and an SSH key pair. Skip state-bucket creation for an existing installation.

```bash
terraform -chdir=terraform/bootstrap init
terraform -chdir=terraform/bootstrap apply -var='state_bucket_name=YOUR-UNIQUE-STATE-BUCKET'
cp terraform/backend.hcl.example terraform/backend.hcl
cp terraform/terraform.tfvars.example terraform/terraform.tfvars
```

Edit `backend.hcl` with the state bucket name. Edit `terraform.tfvars` with a unique
`name_prefix`, your public key text, and allowed SSH IPv4 CIDRs. For example:

```hcl
name_prefix       = "YOUR-UNIQUE-PREFIX"
ssh_public_key    = "ssh-ed25519 YOUR-PUBLIC-KEY optional-comment"
ssh_allowed_cidrs = ["YOUR-PUBLIC-IP/32"]
```

Find your public key with `cat ~/.ssh/id_ed25519.pub` and current public IPv4 with
`curl -4 https://checkip.amazonaws.com`. Keep the private key local; its usual path is
`~/.ssh/id_ed25519`, without `.pub`. If your public IP changes, update the CIDR and apply.
Only SSH from the configured CIDRs is allowed; TCP 80/443 are public.

Run the init/plan/apply sequence above, then [deploy and initialize data](../deploy/README.md).
The bootstrap state is local: securely back up `terraform/bootstrap/terraform.tfstate`
after changes. The main backend uses S3-native locking. Keep credentials out of backend files,
and never commit Terraform state.

## Porkbun DNS

Read the required values:

```bash
terraform -chdir=terraform output
```

| Type | Porkbun host | Value |
|---|---|---|
| A | Leave blank (apex) | `static_ip_address` output |
| CNAME | `www` | `thebotradio.com` |

Remove conflicting website ALIAS/A/CNAME records and stale AAAA records for these hosts.
Keep mail and unrelated records. The instance is IPv4-only. Caddy obtains certificates once
DNS points to the instance; allow existing DNS caches to expire. See
[verification and troubleshooting](../deploy/README.md#verify-the-site).

## Data and backups

The website keeps personas and input descriptions in `/var/lib/the-bot-radio/data` on the
instance. Daily records can come from local files or the S3 memory cache. Code deployments
preserve the local data; instance replacement does not. There is no automatic backup or
local-to-S3 synchronization. Daily generation runs at 06:00 America/New_York; see
[generator rollout](../deploy/generation.md).

The retained bucket stays at `aws_s3_bucket.site["data"]`, with encryption, versioning,
public-access blocking, ownership controls, lifecycle rules, TLS-only access, and destruction
protection. The state backend and its bootstrap are separate from application data.

## Website S3 identity

Terraform creates `${var.name_prefix}-web` without a console login. Its inline policy permits
`s3:ListBucket` only with `s3:prefix` exactly `production/days/`, and `s3:GetObject` only on
the managed data bucket's `production/days/*`. With the current staging name prefix, this
is `s3://the-bot-radio-staging-data/production/days/`. Bucket policies and ECS roles are
unchanged. The bucket and user must be in the same AWS account.

No access-key resource is configured. Create and rotate keys outside Terraform so secrets
never enter configuration, outputs, plans, or state. After applying the reviewed IAM change,
use [website S3 setup](../deploy/README.md#initial-setup) to create a private temporary key
file and run `deploy/configure-web-s3.sh`. The same command configures future instances
after local metadata is initialized. It installs a systemd drop-in without changing Lightsail
bootstrap or triggering replacement. See the deployment guide for verification, rotation,
automatic/manual rollback, and cache-freshness checks.

The October 28, 2026 reconciliation preserves the deployed generator's S3 environment and
application-data policy, along with the existing environment and website-user ARN outputs.
The bootstrap matches the instance's original user data; configure website S3 access through
the deployment command. A full refreshed plan confirmed no resource or output changes.

## Local checks

```bash
terraform fmt -check -recursive terraform
terraform -chdir=terraform init -backend=false
terraform -chdir=terraform validate
terraform -chdir=terraform test
sh -n terraform/templates/bootstrap.sh
uv tool run --from shellcheck-py shellcheck terraform/templates/bootstrap.sh
```

Terraform tests mock AWS and create no cloud resources. The deployment tests also check that
Lightsail's shell wrapper hands the bootstrap to Bash; see [deployment checks](../deploy/README.md#local-checks).

## Daily generator

The root configuration also stages dedicated ECS Fargate, ECR, networking, logging, IAM,
Secrets Manager metadata, and a Scheduler dead-letter queue. `generation_image_uri` defaults
to null, so the foundation can be applied before the first image exists. Supplying a digest
creates the task definition and disabled 06:00 New York schedule. Keep
`generation_schedule_enabled = false` through this stage. Follow the
[generator rollout and manual smoke check](../deploy/generation.md) for secret setup, image
releases, IAM review, failure inspection, and rollback.
