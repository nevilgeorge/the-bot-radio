#!/usr/bin/env bash
# Run as root. Retain the immediately preceding configuration for rollback.
set +x
set -euo pipefail
umask 077
[[ $# == 1 && -f "$1" && ! -L "$1" ]] || { echo 'Expected staged environment file' >&2; exit 2; }
environment_source="$1"
configuration_root=/etc/the-bot-radio
drop_in_directory=/etc/systemd/system/the-bot-radio.service.d
state_root=/var/lib/the-bot-radio
environment_file="$configuration_root/web.env"
drop_in_file="$drop_in_directory/20-web-s3.conf"
exec 9>"$state_root/deploy.lock"
flock -n 9 || { echo 'Another deployment is in progress' >&2; exit 1; }
install -d -o root -g root -m 700 "$configuration_root" "$configuration_root/backups"
install -d -o root -g root -m 755 "$drop_in_directory"
backup_directory="$(mktemp -d "$configuration_root/backups/web-s3.XXXXXXXX")"
for configuration in "$environment_file" "$drop_in_file"; do
  if [[ -e "$configuration" || -L "$configuration" ]]; then
    [[ -f "$configuration" && ! -L "$configuration" ]] || { echo 'Configuration must be a regular file' >&2; exit 1; }
    cp -p -- "$configuration" "$backup_directory/$(basename "$configuration")"
  fi
done
healthy() {
  local attempt
  for ((attempt=1; attempt<=15; attempt++)); do
    if systemctl is-active --quiet the-bot-radio.service && \
      curl --fail --silent --show-error --max-time 2 http://127.0.0.1:8000/healthz >/dev/null; then
      return 0
    fi
    sleep 2
  done
  return 1
}
configuration_changed=false
verified=false
cleanup() {
  local status=$?
  trap - EXIT
  rm -f -- "$environment_file.next" "$drop_in_file.next"
  if $configuration_changed && ! $verified; then
    echo 'S3 activation failed; restoring previous configuration' >&2
    for configuration in "$environment_file" "$drop_in_file"; do
      if [[ -f "$backup_directory/$(basename "$configuration")" ]]; then
        cp -p -- "$backup_directory/$(basename "$configuration")" "$configuration.next"
        mv -f -- "$configuration.next" "$configuration"
      else
        rm -f -- "$configuration"
      fi
    done
    if ! systemctl daemon-reload || ! systemctl restart the-bot-radio.service || ! healthy; then
      echo 'Previous configuration did not recover; inspect the systemd journal' >&2
    fi
  fi
  echo "Previous configuration retained at $backup_directory"
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
configuration_changed=true
install -o root -g root -m 600 "$environment_source" "$environment_file.next"
mv -f -- "$environment_file.next" "$environment_file"
printf '[Service]\nEnvironmentFile=%s\n' "$environment_file" > "$drop_in_file.next"
chown root:root "$drop_in_file.next"
chmod 644 "$drop_in_file.next"
mv -f -- "$drop_in_file.next" "$drop_in_file"
systemctl daemon-reload
systemctl restart the-bot-radio.service
healthy
verified=true
echo 'S3 configuration activated and website health verified.'
