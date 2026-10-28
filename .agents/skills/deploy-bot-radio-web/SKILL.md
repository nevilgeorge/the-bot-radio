---
name: deploy-bot-radio-web
description: Deploy or redeploy The Bot Radio web app to its existing AWS Lightsail instance using the repository's SSH release script, then verify public health and pages. Use for website code releases and deployment preflight; ECS generator releases, infrastructure provisioning, and S3 credential changes are separate workflows.
---

# Deploy The Bot Radio website

Use this repository's existing deployment scripts. A request to deploy authorizes the normal
upload, release activation, service restart, and verification; do not ask again for authorized
steps. Honor tool approval requirements. Creating this skill does not authorize deployment.
For a walkthrough, give instructions; for a request limited to preflight, stop before deployment.

## Prepare the release

Locate the repository root from this skill's location or the current checkout. Read `AGENTS.md`,
`deploy/README.md`, `deploy/deploy-site.sh`, `deploy/remote-deploy.sh`, and `deploy/package.py`.
Use `terraform/README.md` for state/SSH troubleshooting. Current scripts and repository
instructions take precedence over examples here.

Run commands from the repository root. Requirements: Bash, Python 3, Terraform with existing
AWS state access, OpenSSH (`ssh`, `scp`), and the matching SSH private key. Local checks use uv.
Use the user's existing AWS profile; never print credentials or full Terraform state.

1. Check `git status --short --branch`, HEAD, and remotes. For a requested latest upstream
   release, fast-forward the intended branch when the checkout is clean. On main:

   ```bash
   git pull --ff-only origin main
   ```

   Explicit remote/branch arguments handle this checkout's potentially missing upstream.
   Do not reset changes, switch away from user work, or silently deploy a dirty checkout.
   The deploy script packages current local files, including allowlisted untracked source;
   it does not fetch code or deploy a Git commit directly. Explain which local edits will
   ship if the user has explicitly requested deploying them.
2. Run these checks unless successful results already exist for this exact source tree in
   the session:

   ```bash
   uv run ruff check .
   uv run ruff format --check .
   uv run pytest
   ```

   Investigate failures and resolve relevant regressions before deployment. Missing historical
   fixtures under `data/days/` can fail tests without demonstrating an application regression;
   establish and report the cause, and verify the deployment/web behavior with relevant checks.
   Do not invent fixtures, hide failures, or claim the full suite passed. If deployment scripts
   changed, run their documented shell and deployment checks too. Do not run live-provider tests
   as a routine release check.
3. Confirm existing state access and obtain the target from Terraform:

   ```bash
   terraform -chdir=terraform output -raw static_ip_address
   terraform -chdir=terraform output -raw ssh_user
   ```

   If initialization is required, use
   `terraform -chdir=terraform init -backend-config=backend.hcl` with the existing backend.
   Do not invent backend settings or create new infrastructure. A routine code deployment reads
   Terraform outputs; it needs no plan/apply. DNS/firewall/IAM changes and instance replacement
   require separate scope. Never hard-code a previously observed IP.
4. Confirm the SSH key from session context or existing configuration; the usual path here is
   `~/.ssh/id_ed25519`. Inspect `ssh-add -l`; load a passphrase key with `ssh-add` if needed,
   leaving secret entry to the user. Test a connection that exits immediately:

   ```bash
   ssh -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=yes \
     -i ~/.ssh/id_ed25519 USER@STATIC-IP true
   ```

   Substitute Terraform's user and IP. On an unknown host, verify its fingerprint through a
   trusted channel such as the Lightsail console before recording it. Never disable host-key
   verification or remove a changed key blindly. A timeout may mean the user's current public
   IPv4 is absent from `ssh_allowed_cidrs`; inspect before proposing infrastructure changes.

## Deploy

Record the current `/opt/the-bot-radio/current` symlink and public health response as a baseline.
Run the existing command, choosing the verified key or omitting the flag when SSH configuration
already selects it:

```bash
./deploy/deploy-site.sh --identity-file ~/.ssh/id_ed25519
```

For an existing server omit `--seed-data`. The script waits for cloud-init, acquires a remote
deployment lock, uploads the allowlisted application, installs locked production dependencies
in a new release, switches `current`, restarts systemd, and checks localhost `/healthz`.
It preserves persistent data and installed S3 environment/drop-in configuration. Do not run
the generator, publish Spotify playlists, rotate credentials, or reconfigure S3 for a code update.

Seeding is only for an explicitly requested new empty installation. Follow the documented
`--seed-data --data-dir PATH` flow; it copies `inputs.json` and valid daily files and refuses
existing data. If seeding succeeded but activation failed, retry without seeding. Never overwrite
data to get past a seed refusal.

## Verify and recover

Require script success, then check both public endpoints:

```bash
curl --fail --show-error https://thebotradio.com/healthz
curl --fail --show-error https://www.thebotradio.com/healthz
```

Check the homepage, `/index`, an existing `/index/YYYY-MM-DD` link when data exists, and the
specific behavior changed by this release. Inspect styling, archive filters, and share controls
when relevant. Confirm private paths such as `/.env`, `/pyproject.toml`, and `/data/personas.json`
and an unknown route return 404. An empty dataset can validly report `latest_day: null`.
Health confirms availability, not S3 freshness: compare expected records and inspect logs when
freshness matters; S3 refresh waits 60 seconds after each attempt.

On a failed restart or health check, the script attempts to restore the previous release.
On a failed first deployment it stops the service. Verify the actual symlink and service/health
before reporting rollback success. Inspect over SSH:

```bash
sudo journalctl -u the-bot-radio.service -n 100 --no-pager
sudo journalctl -u caddy -n 100 --no-pager
sudo cloud-init status --long
sudo tail -n 100 /var/log/cloud-init-output.log
```

If SSH disconnects during activation, inspect server state before retrying. Respect the remote
lock; do not delete locks or prune releases automatically. Fix the identified problem and retry
only when understood. Public TLS/DNS failure after local success needs diagnosis, not an assumed
application rollback. Never replace the instance or restore data as routine failure recovery.

Report the source commit/local changes, target host, check results, activated release ID,
public health and page verification, and any unresolved failure. Do not claim a deployment if
only preflight ran, or claim visual verification based on HTTP status alone.
