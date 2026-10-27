#!/usr/bin/env bash
# Deploy the application over SSH; never apply Terraform or copy repository secrets.
set -euo pipefail
repository_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
data_dir="$repository_root/data"
seed_data=false
identity_file=""
while (($#)); do
  case "$1" in
    --seed-data) seed_data=true; shift ;;
    --data-dir|--identity-file)
      [[ $# -ge 2 && -n "$2" ]] || { echo "$1 requires a path" >&2; exit 2; }
      if [[ "$1" == --data-dir ]]; then data_dir="$2"; else identity_file="$2"; fi
      shift 2 ;;
    -h|--help)
      echo 'Usage: deploy/deploy-site.sh [--identity-file PATH] [--seed-data] [--data-dir PATH]'
      exit 0 ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
done
for executable in terraform python3 ssh scp; do
  command -v "$executable" >/dev/null || { echo "Missing executable: $executable" >&2; exit 1; }
done
work_dir="$(mktemp -d "${TMPDIR:-/tmp}/the-bot-radio-deploy.XXXXXXXX")"
trap 'rm -rf -- "$work_dir"' EXIT
terraform -chdir="$repository_root/terraform" output -json > "$work_dir/outputs.json"
output_value() {
  python3 - "$work_dir/outputs.json" "$1" <<'PY'
import json
import sys

with open(sys.argv[1]) as output_file:
    value = json.load(output_file)[sys.argv[2]]["value"]
if not isinstance(value, str) or not value.strip():
    raise SystemExit(f"Invalid Terraform output: {sys.argv[2]}")
print(value)
PY
}
ssh_host="$(output_value static_ip_address)"
ssh_user="$(output_value ssh_user)"
# These outputs enter remote command arguments. Restrict their alphabet before use.
[[ "$ssh_host" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ && "$ssh_user" =~ ^[a-z_][a-z0-9_-]*$ ]] || {
  echo 'Invalid SSH host or user Terraform output' >&2; exit 1;
}
ssh_target="$ssh_user@$ssh_host"
ssh_options=(-o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=15 -o ServerAliveCountMax=3)
if [[ -n "$identity_file" ]]; then ssh_options+=(-i "$identity_file"); fi
python3 "$repository_root/deploy/package.py" "$repository_root" "$work_dir" "$seed_data" "$data_dir"
# cloud-init may still be creating the SSH user. Do not disable SSH host key verification.
ready=false
for ((attempt=1; attempt<=30; attempt++)); do
  if ssh "${ssh_options[@]}" "$ssh_target" true; then ready=true; break; fi
  sleep 2
done
$ready || { echo 'Instance SSH did not become ready' >&2; exit 1; }
if ! ssh "${ssh_options[@]}" "$ssh_target" \
  'sudo timeout 900 cloud-init status --wait && sudo test -f /var/lib/the-bot-radio/bootstrap-complete'; then
  echo 'Instance initialization failed or bootstrap-complete marker is missing. Setup diagnostics:' >&2
  ssh "${ssh_options[@]}" "$ssh_target" \
    'sudo cloud-init status --long; sudo tail -n 100 /var/log/cloud-init-output.log' >&2 || true
  exit 1
fi
release_id="$(python3 -c 'import uuid; print(uuid.uuid4().hex)')"
remote_stage="/tmp/the-bot-radio-$release_id"
# Remote paths contain only a fixed prefix and a generated hexadecimal UUID.
# shellcheck disable=SC2029
ssh "${ssh_options[@]}" "$ssh_target" "umask 077; mkdir '$remote_stage'"
# Best-effort cleanup also runs when transfer or activation fails.
cleanup() {
  # shellcheck disable=SC2029
  ssh "${ssh_options[@]}" "$ssh_target" "rm -rf -- '$remote_stage'" || true
  rm -rf -- "$work_dir"
}
trap cleanup EXIT
artifacts=("$work_dir/application.tar.gz" "$repository_root/deploy/remote-deploy.sh")
if $seed_data; then artifacts+=("$work_dir/seed.tar.gz"); fi
scp "${ssh_options[@]}" "${artifacts[@]}" "$ssh_target:$remote_stage/"
# shellcheck disable=SC2029
ssh "${ssh_options[@]}" "$ssh_target" \
  "sudo bash '$remote_stage/remote-deploy.sh' '$remote_stage' '$release_id' '$seed_data'"
echo 'Website deployment complete.'
