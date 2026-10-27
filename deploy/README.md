# Deploying the application

Run commands from the repository root. Infrastructure setup and changes are covered in
[Lightsail hosting](../terraform/README.md).

## Deploy code

Requirements: Bash, Python 3, Terraform with AWS state access, OpenSSH (`ssh`, `scp`),
and the private key matching the configured `ssh_public_key`. Set `AWS_PROFILE` if needed.

If your key has a passphrase, load it into your SSH agent first:

```bash
ssh-add ~/.ssh/id_ed25519
```

On your first connection to an instance, connect interactively, verify its host fingerprint
through a trusted channel such as the Lightsail console, then exit:

```bash
terraform -chdir=terraform output -raw static_ip_address
ssh -i ~/.ssh/id_ed25519 ubuntu@STATIC-IP
```

Deploy:

```bash
./deploy/deploy-site.sh --identity-file ~/.ssh/id_ed25519
```

Replace the key path if you use a different key. You can omit `--identity-file` when your
SSH agent or configuration selects the right key. Deployment disables interactive prompts.

The script waits for instance setup, installs locked production dependencies in a new
release, switches the `current` symlink, restarts systemd, and checks `/healthz`. A failed
restart or health check restores the previous release and exits unsuccessfully. On a failed
first deployment, it stops the service. A brief connection failure during startup retries
can appear before a successful health check.

## Initialize data once

For a new, empty server, deploy with your source dataset:

```bash
./deploy/deploy-site.sh \
  --identity-file ~/.ssh/id_ed25519 \
  --seed-data \
  --data-dir ./data
```

Only `personas.json`, `inputs.json`, and calendar-valid `days/YYYY-MM-DD.json` files are
seeded. JSON must parse; symlinks are rejected. Existing data is never overwritten.
Relative paths are relative to your current directory.

If the seed succeeds but application activation fails, retry without `--seed-data`.
Routine code deployments do not read local data or modify remote data, and they do not
publish new daily records. The site reloads records when files change on the server.

## Verify the site

After deployment, check both domains:

```bash
curl --fail https://thebotradio.com/healthz
curl --fail https://www.thebotradio.com/healthz
```

Open the homepage, a real `/index/YYYY-MM-DD` link, and archive filters. Check styling and
share controls. Submit the subscription form and confirm its redirect; it currently validates
the address and shows confirmation, without enrolling it in a mailing provider.
Confirm unknown routes and private paths such as `/data/personas.json`, `/.env`, and
`/pyproject.toml` return 404.

Before DNS is configured, view the app through a tunnel. Leave this command running and
open `http://localhost:8001`:

```bash
ssh -N -L 8001:127.0.0.1:8000 -i ~/.ssh/id_ed25519 ubuntu@STATIC-IP
```

## Logs and troubleshooting

SSH into the instance and run:

```bash
sudo journalctl -u the-bot-radio.service -n 100 --no-pager
sudo journalctl -u caddy -n 100 --no-pager
sudo cloud-init status --long
sudo tail -n 100 /var/log/cloud-init-output.log
```

- **Permission denied (publickey):** check the private key path and run `ssh-add` if it has
  a passphrase. Test with `ssh -o BatchMode=yes -i ~/.ssh/id_ed25519 ubuntu@STATIC-IP true`.
- **SSH timeout:** confirm the instance is running, the static IP is attached, and your
  current public IPv4 is in `ssh_allowed_cidrs` as a `/32`. Apply any Terraform changes.
- **Initialization failed:** inspect cloud-init status and logs; deployment also prints
  setup diagnostics. Fix the cause before retrying deployment.
- **HTTPS unavailable:** check Porkbun records and Caddy logs. DNS must point to the instance
  before certificates can be issued. Caddy retries after earlier DNS failures.
- **Seed refused:** the server already contains data. Retry without `--seed-data`.

## Server layout

| Path | Purpose |
|---|---|
| `/opt/the-bot-radio/releases/<id>` | Application releases and their virtual environments |
| `/opt/the-bot-radio/current` | Active release symlink |
| `/var/lib/the-bot-radio/data` | Persistent personas, input descriptions, and daily records |

The app runs as `bot-radio` on `127.0.0.1:8000` and trusts forwarded headers only from
localhost. Caddy serves HTTPS and proxies all routes, including static assets. Only the
application's rendered pages and static assets are exposed, not its data directory.

Code packages contain source, approved assets, dependency manifests, the lockfile, and
required build metadata. Credentials, repository metadata, environments, and data are
excluded; `deploy/package.py` defines the allowlist. A remote lock prevents overlapping
deployments. Previous and failed releases remain on disk; pruning is manual.

Data survives code deployments, but lives on the instance disk. Back it up before replacing
or terminating the instance. The retained S3 data bucket is not synchronized automatically.
Automated backups, playlist generation scheduling, and high availability are not configured.

## Local checks

```bash
bash -n deploy/deploy-site.sh deploy/remote-deploy.sh
uv tool run --from shellcheck-py shellcheck deploy/deploy-site.sh deploy/remote-deploy.sh
uv run pytest tests/test_deploy_site.py -q
```

Tests mock external commands and cover packaging, seeding, initialization failures,
data preservation, rollback, and concurrent deployment rejection.
