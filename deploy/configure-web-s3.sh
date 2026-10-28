#!/usr/bin/env bash
# Configure an existing website; credentials are read from a private JSON file only.
set +x
set -euo pipefail
umask 077
repository_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
credentials_file=""
identity_file=""
while (($#)); do
  case "$1" in
    --credentials-file|--identity-file)
      [[ $# -ge 2 && -n "$2" ]] || { echo "$1 requires a path" >&2; exit 2; }
      if [[ "$1" == --credentials-file ]]; then credentials_file="$2"; else identity_file="$2"; fi
      shift 2 ;;
    -h|--help)
      echo 'Usage: deploy/configure-web-s3.sh --credentials-file PATH [--identity-file PATH]'
      echo 'PATH must be a private JSON file from aws iam create-access-key.'
      exit 0 ;;
    *) echo 'Unknown argument' >&2; exit 2 ;;
  esac
done
[[ -n "$credentials_file" ]] || { echo '--credentials-file is required' >&2; exit 2; }
for executable in terraform python3 ssh scp; do
  command -v "$executable" >/dev/null || { echo "Missing executable: $executable" >&2; exit 1; }
done
work_dir="$(mktemp -d "${TMPDIR:-/tmp}/the-bot-radio-web-s3.XXXXXXXX")"
trap 'rm -rf -- "$work_dir"' EXIT
python3 - "$credentials_file" "$work_dir/web.env" <<'PY'
import json
import os
import re
import stat
import sys

try:
    descriptor = os.open(sys.argv[1], os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor) as credentials_file:
        metadata = os.fstat(credentials_file.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077:
            raise ValueError
        credentials = json.load(credentials_file)["AccessKey"]
    access_key = credentials["AccessKeyId"]
    secret_key = credentials["SecretAccessKey"]
    if not isinstance(access_key, str) or not re.fullmatch(r"AKIA[A-Z0-9]{16}", access_key):
        raise ValueError
    if not isinstance(secret_key, str) or not re.fullmatch(r"[A-Za-z0-9/+=]{40}", secret_key):
        raise ValueError
    with open(sys.argv[2], "x", opener=lambda path, flags: os.open(path, flags, 0o600)) as output:
        output.write(f"AWS_ACCESS_KEY_ID={access_key}\nAWS_SECRET_ACCESS_KEY={secret_key}\n")
        output.write("AWS_DEFAULT_REGION=us-east-1\nAWS_REGION=us-east-1\n")
        output.write("BOT_RADIO_S3_BUCKET=the-bot-radio-staging-data\nBOT_RADIO_S3_PREFIX=production\n")
except (OSError, ValueError, KeyError, TypeError):
    raise SystemExit("Invalid credentials file: require private regular JSON file with an IAM access key") from None
PY
terraform -chdir="$repository_root/terraform" output -json > "$work_dir/outputs.json"
output_value() {
  python3 - "$work_dir/outputs.json" "$1" <<'PY'
import json
import sys

with open(sys.argv[1]) as output_file:
    value = json.load(output_file)[sys.argv[2]]["value"]
if not isinstance(value, str) or not value.strip():
    raise SystemExit("Invalid Terraform SSH output")
print(value)
PY
}
ssh_host="$(output_value static_ip_address)"
ssh_user="$(output_value ssh_user)"
[[ "$ssh_host" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ && "$ssh_user" =~ ^[a-z_][a-z0-9_-]*$ ]] || {
  echo 'Invalid SSH host or user Terraform output' >&2; exit 1;
}
ssh_target="$ssh_user@$ssh_host"
ssh_options=(-o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=15 -o ServerAliveCountMax=3)
if [[ -n "$identity_file" ]]; then ssh_options+=(-i "$identity_file"); fi
release_id="$(python3 -c 'import uuid; print(uuid.uuid4().hex)')"
remote_stage="/tmp/the-bot-radio-web-s3-$release_id"
cleanup() {
  # shellcheck disable=SC2029
  ssh "${ssh_options[@]}" "$ssh_target" "rm -rf -- '$remote_stage'" || true
  rm -rf -- "$work_dir"
}
trap cleanup EXIT
# shellcheck disable=SC2029
ssh "${ssh_options[@]}" "$ssh_target" "umask 077; mkdir '$remote_stage'"
scp "${ssh_options[@]}" "$work_dir/web.env" "$repository_root/deploy/remote-configure-web-s3.sh" \
  "$ssh_target:$remote_stage/"
# shellcheck disable=SC2029
ssh "${ssh_options[@]}" "$ssh_target" \
  "sudo bash '$remote_stage/remote-configure-web-s3.sh' '$remote_stage/web.env'"
echo 'Website S3 configuration complete. Remove the local credential file after verification.'
