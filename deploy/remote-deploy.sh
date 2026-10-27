#!/usr/bin/env bash
# Run as root on the instance, after cloud-init succeeds.
set -euo pipefail
[[ $# == 3 ]] || { echo 'Expected staging directory, release ID, and seed flag' >&2; exit 2; }
staging_directory="$1"
release_id="$2"
seed_data="$3"
[[ "$release_id" =~ ^[a-f0-9]{32}$ && "$seed_data" =~ ^(true|false)$ ]] || exit 2
app_root=/opt/the-bot-radio
state_root=/var/lib/the-bot-radio
export UV_PYTHON_INSTALL_DIR=/opt/uv/python
exec 9>"$state_root/deploy.lock"
flock -n 9 || { echo 'Another deployment is in progress' >&2; exit 1; }
release_directory="$app_root/releases/$release_id"
seed_stage=""
cleanup() {
  if [[ -n "$seed_stage" && -d "$seed_stage" ]]; then rm -rf -- "$seed_stage"; fi
}
trap cleanup EXIT
if [[ "$seed_data" == true ]]; then
  # An empty bootstrap directory is safe to initialize. Any content means initialized.
  if [[ -L "$state_root/data" || -n "$(find "$state_root/data" -mindepth 1 -print -quit)" ]]; then
    echo 'Refusing --seed-data: an initialized dataset already exists' >&2; exit 1
  fi
  seed_stage="$(mktemp -d "$state_root/seed.XXXXXXXX")"
  tar --extract --gzip --file "$staging_directory/seed.tar.gz" --directory "$seed_stage" \
    --no-same-owner --no-same-permissions
  chown -R bot-radio:bot-radio "$seed_stage"
  chmod 750 "$seed_stage"
fi
mkdir "$release_directory"
tar --extract --gzip --file "$staging_directory/application.tar.gz" \
  --directory "$release_directory" --no-same-owner --no-same-permissions
chown -R bot-radio:bot-radio "$release_directory"
# Build in the final path: virtualenv entry points contain absolute paths.
runuser -u bot-radio -- env UV_PYTHON_INSTALL_DIR="$UV_PYTHON_INSTALL_DIR" \
  /usr/local/bin/uv sync --project "$release_directory" --locked --no-dev --no-editable --python 3.13
previous_release="$(readlink "$app_root/current" || true)"
if [[ -n "$seed_stage" ]]; then
  # rmdir refuses if anything appeared since the guard; rename stays on the same filesystem.
  rmdir "$state_root/data"
  if ! mv -T "$seed_stage" "$state_root/data"; then
    install -d -o bot-radio -g bot-radio -m 750 "$state_root/data"
    exit 1
  fi
  seed_stage=""
fi
activate() {
  ln -s "$1" "$app_root/current.$release_id.next"
  mv -Tf "$app_root/current.$release_id.next" "$app_root/current"
}
healthy() {
  local attempt
  for ((attempt=1; attempt<=15; attempt++)); do
    if curl --fail --silent --show-error --max-time 2 http://127.0.0.1:8000/healthz; then
      return 0
    fi
    sleep 2
  done
  return 1
}
# Roll back restart errors as well as failed HTTP checks.
activate "$release_directory"
if systemctl restart the-bot-radio.service && healthy; then
  echo "Activated release $release_id"
else
  echo 'Deployment health check failed; restoring previous release' >&2
  if [[ -n "$previous_release" ]]; then
    activate "$previous_release"
    if ! systemctl restart the-bot-radio.service || ! healthy; then
      echo 'Previous release did not recover; inspect the systemd journal' >&2
    fi
  else
    rm -- "$app_root/current"
    systemctl stop the-bot-radio.service || true
  fi
  exit 1
fi
