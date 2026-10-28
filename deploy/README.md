# Deploying the application

Run commands from the repository root. Infrastructure setup and changes are covered in
[Lightsail hosting](../terraform/README.md). For the staged ECS daily generator, see
[generator releases and rollout](generation.md). Trigger a one-off task with
`./deploy/generate-remote.py`, using the same arguments as `the-bot-radio-generate`;
see [remote invocation](generation.md#one-off-remote-generator).

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

Only `inputs.json` and calendar-valid `days/YYYY-MM-DD.json` files are
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
<<<<<<< HEAD
| `/var/lib/the-bot-radio/data` | Persistent input descriptions and daily records |
=======
| `/var/lib/the-bot-radio/data` | Persistent personas, input descriptions, and daily records |
| `/etc/the-bot-radio/web.env` | Root-only website S3 credentials and settings |
| `/etc/systemd/system/the-bot-radio.service.d/20-web-s3.conf` | Loads the environment before starting as `bot-radio` |
| `/etc/the-bot-radio/backups/web-s3.*` | Root-only previous environment and drop-in configurations |
>>>>>>> f9ba99c (Add Terraform-managed credentials for Lightsail to read from S3)

The app runs as `bot-radio` on `127.0.0.1:8000` and trusts forwarded headers only from
localhost. Caddy serves HTTPS and proxies all routes, including static assets. Only the
application's rendered pages and static assets are exposed, not its data directory.

Code packages contain source, approved assets, dependency manifests, the lockfile, and
required build metadata. Credentials, repository metadata, environments, and data are
excluded; `deploy/package.py` defines the allowlist. A remote lock prevents overlapping
deployments. Previous and failed releases remain on disk; pruning is manual.

Local metadata and local-mode daily records survive code deployments, but live on the instance
disk. Back them up before replacing or terminating the instance. S3 mode reads daily records
directly from the retained bucket; it does not synchronize them to the instance disk.
Automated backups, playlist generation scheduling, and high availability are not configured.

## Local checks

```bash
bash -n deploy/deploy-site.sh deploy/remote-deploy.sh \
  deploy/configure-web-s3.sh deploy/remote-configure-web-s3.sh
uv tool run --from shellcheck-py shellcheck deploy/deploy-site.sh deploy/remote-deploy.sh \
  deploy/configure-web-s3.sh deploy/remote-configure-web-s3.sh
uv run pytest tests/test_deploy_site.py tests/test_configure_web_s3.py -q
```

Tests mock external commands and cover packaging, seeding, initialization failures,
data preservation, rollback, concurrent deployment rejection, credential redaction,
private file permissions, systemd configuration, and repeat S3 configuration.


## Optional S3 daily reads

The dedicated configuration command enables existing or future instances to read
`s3://the-bot-radio-staging-data/production/days/` in `us-east-1`. It requires the same
Terraform state access and SSH setup as code deployment. It does not change Lightsail
bootstrap/user data or deploy application code.

Terraform manages `${name_prefix}-web`, a user without a console login. Its inline policy
allows only `s3:ListBucket` with the exact `production/days/` prefix and `s3:GetObject` on
`production/days/*`. Artifact reads, root listing, writes, and deletion are not granted.
Keep generation and qualification credentials separate. Access keys are created outside
Terraform and must never enter its configuration, outputs, plans, or state.

<<<<<<< HEAD
`personas.json` and `shared_catalog.jsonl` ship in the application package under
`the_bot_radio/resources/`. Keep `inputs.json` under `BOT_RADIO_DATA_DIR` even in S3 mode. The initial remote load must succeed before
startup completes. Empty or entirely hidden/invalid data is allowed. Later refreshes wait
60 seconds after the preceding attempt, retain the last complete snapshot on infrastructure
failure, and log failures/recovery. `/healthz` reports the latest cached visible day and
continues returning its existing successful response even when that cache is stale. Check
application logs when investigating freshness. Shutdown drains workers before closing the SDK.
=======
### Initial setup
>>>>>>> f9ba99c (Add Terraform-managed credentials for Lightsail to read from S3)

1. Confirm the AWS account owns the selected bucket before applying the IAM configuration.
   Use `aws sts get-caller-identity` and `aws s3api head-bucket` with
   `--expected-bucket-owner ACCOUNT-ID`. Review the Terraform plan: the S3 credential rollout
   should add only `aws_iam_user.web` and `aws_iam_user_policy.web`, plus the username output.
   A full plan can include unrelated drift, including instance replacement; do not apply it
   blindly for a credential change. See [infrastructure updates](../terraform/README.md#update-existing-infrastructure).
2. Inspect current service health, visible day, and logs. Confirm
   `/var/lib/the-bot-radio/data/personas.json` and `inputs.json` exist. Keep both files local
   in S3 mode; generation's catalog also stays local.
3. List `production/days/` and validate the selected daily records before activation. Record
   the newest visible date expected on the site. An empty prefix is supported and produces
   an empty homepage/archive with `latest_day: null`; configuration does not copy local data
   or generate records. During the October 28, 2026 preflight this prefix was empty, and the
   existing service was restarting after S3 `AccessDenied` errors with no installed credentials.
   Recheck these conditions before a later rollout.
4. After the reviewed IAM plan is applied, create a key directly into a private file:

   ```bash
   umask 077
   key_directory="$(mktemp -d "${TMPDIR:-/tmp}/the-bot-radio-web-key.XXXXXXXX")"
   web_user="$(terraform -chdir=terraform output -raw web_iam_user_name)"
   aws iam create-access-key --user-name "$web_user" --output json > "$key_directory/access-key.json"
   ./deploy/configure-web-s3.sh \
     --credentials-file "$key_directory/access-key.json" \
     --identity-file ~/.ssh/id_ed25519
   ```

   The file must be a private regular JSON file containing the `create-access-key` response;
   symlinks, public file modes, and malformed credentials are rejected. Do not display it,
   pass secrets as arguments, or enable tracing around credential handling. The command
   privately transfers the credentials, atomically installs root-owned `0600` `web.env`, and
   installs the systemd `EnvironmentFile` drop-in. Systemd reads it before launching the
   process as `bot-radio`. A shared deployment lock prevents concurrent activation.
5. Verify the routes, expected records, and startup logs using the checks below. After
   successful installation and verification, remove the local temporary credential file:

   ```bash
   rm -- "$key_directory/access-key.json"
   rmdir -- "$key_directory"
   ```

Use this same command after deploying and initializing metadata on a future instance.
Routine code deployments preserve the installed environment and drop-in.

### Verification and cache freshness

List the exact allowed prefix with operator credentials:

```bash
aws s3api list-objects-v2 --bucket the-bot-radio-staging-data \
  --prefix production/days/ --region us-east-1 \
  --query 'Contents[].{Key:Key,Modified:LastModified}'
curl --fail https://thebotradio.com/healthz
curl --fail https://thebotradio.com/
curl --fail https://thebotradio.com/index
```

Also verify listing and a daily object read using the installed website credentials on the
instance, without printing the environment file. Compare the validated S3 record's date and
contents with the homepage, archive, and `/index/YYYY-MM-DD` route. Inspect
`sudo journalctl -u the-bot-radio.service --since '10 minutes ago' --no-pager` for startup,
invalid-record warnings, S3 errors, and refresh recovery. An empty listing has no daily object
to read; verify the empty-state pages and `latest_day: null` instead.

The initial remote load must succeed before startup completes. Empty or entirely
hidden/invalid data is allowed. Later refreshes wait 60 seconds after the preceding attempt,
retain the entire last complete snapshot on infrastructure failure, and log failures/recovery.
`/healthz` reports the latest cached visible day and remains successful when the cache is
stale. It proves availability, not freshness. Compare S3 with rendered records and review logs
after at least one refresh interval when checking later updates. Shutdown drains workers
before closing the SDK.

### Credential rotation

Create a replacement access key into a new private file, rerun the configuration command,
and verify S3 listing/reading, expected pages, and logs before deactivating the old key:

```bash
aws iam update-access-key --user-name "$web_user" \
  --access-key-id OLD-ACCESS-KEY-ID --status Inactive
```

Keep the previous key active during installation and verification so rollback can recover.
Remove the temporary replacement file after verification. Delete the inactive old key only
after the rollback window closes; do not delete the new key. Keys are managed outside
Terraform throughout.

### Roll back configuration

Failed startup or health verification automatically restores the previous environment and
drop-in, reloads systemd, and restarts the previous configuration. The command prints its
root-only backup directory. Successful runs retain the same backup for manual rollback.
An absent file in that backup means the corresponding file did not exist before activation.

For manual rollback, SSH to the instance, select the correct backup directory printed by the
configuration command, and run the following after replacing `REPLACE-ME`. The previous
credential must still be active if that configuration used one.

```bash
sudo bash <<'SH'
set -euo pipefail
backup=/etc/the-bot-radio/backups/web-s3.REPLACE-ME
test -d "$backup"
exec 9>/var/lib/the-bot-radio/deploy.lock
flock -n 9
for destination in /etc/the-bot-radio/web.env \
  /etc/systemd/system/the-bot-radio.service.d/20-web-s3.conf; do
  source="$backup/$(basename "$destination")"
  if test -f "$source"; then
    cp -p -- "$source" "$destination.next"
    mv -f -- "$destination.next" "$destination"
  else
    rm -f -- "$destination"
  fi
done
systemctl daemon-reload
systemctl restart the-bot-radio.service
curl --fail --retry 15 --retry-delay 2 --retry-connrefused http://127.0.0.1:8000/healthz
SH
```

Repeat the public page and freshness checks. Restoring the previous files restores their
settings; it cannot make an already broken prior configuration healthy. Backups contain
credentials: keep them root-only and prune obsolete backups after the rotation rollback
window closes.

For isolated storage qualification, use `RUN_LIVE=1 uv run pytest -m s3_live -s` with an
explicit bucket and separate qualification credentials. Tests retain unique
`qualification/<uuid>/` locations and never delete objects or versions. See
[the S3 dev plan](../docs/dev_plans/daily_files_s3.md) for evidence and limitations.
Publishing, scheduling, daily-data copying, and retention changes are separate work.

### October 28, 2026 activation

The dedicated `the-bot-radio-staging-web` user and exact policy already existed in AWS
and Terraform state. An IAM-only plan/apply confirmed zero resource changes; the full plan
contained unrelated Lightsail replacement and ECS drift and was not applied. The account
and bucket ownership matched. One access key was created outside Terraform, installed as
root-owned `0600` `/etc/the-bot-radio/web.env`, and its temporary local file was removed.
The retained prior-configuration backup is `/etc/the-bot-radio/backups/web-s3.kQPUVyUL`;
that prior configuration was already failing startup and is not a healthy fallback.

Startup completed successfully after installation. Both public domain health endpoints,
the homepage, and `/index` returned HTTP 200. A live listing using the installed website
credentials succeeded with zero objects in `production/days/`, consistent with
`{"ok":true,"latest_day":null}`. No data was copied or generated. Actual object reads and
rendered daily-record acceptance remain unverified until a valid record exists there.
IAM simulation allowed daily listing/reads and denied root listing, artifact reads, writes,
and deletion; no live mutation attempts were used. Subsequent refresh failures still retain
cached data, so health alone does not establish freshness.
