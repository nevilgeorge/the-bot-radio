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

Instance replacement loses its local data. Back up `/var/lib/the-bot-radio/data` before
approving replacement, then restore or seed the new server. Terraform recreates the static IP
attachment and firewall when replacing the instance. The static IP remains the same if its
resource is retained; the SSH host key changes and must be verified again.

## Set up a new installation

Requirements: Terraform >=1.10 and <2, AWS credentials permitted to manage Lightsail and S3,
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

The website reads `/var/lib/the-bot-radio/data` on the instance. Code deployments preserve
it; instance replacement does not. There is no automatic backup, S3 synchronization, or
playlist generation schedule.

The retained bucket stays at `aws_s3_bucket.site["data"]`, with encryption, versioning,
public-access blocking, ownership controls, lifecycle rules, TLS-only access, and destruction
protection. The state backend and its bootstrap are separate from application data.

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
