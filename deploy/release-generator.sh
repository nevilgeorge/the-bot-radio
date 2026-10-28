#!/usr/bin/env bash
# Build, verify, and push an immutable image; never apply Terraform or run generation.
set -euo pipefail
repository_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
if (($#)); then
  if [[ $# == 1 && ( "$1" == --help || "$1" == -h ) ]]; then
    echo 'Usage: deploy/release-generator.sh (prints the ECR digest URI on stdout)'
    exit 0
  fi
  echo 'This script takes no arguments.' >&2
  exit 2
fi
for executable in terraform aws docker git python3; do
  command -v "$executable" >/dev/null || { echo "Missing executable: $executable" >&2; exit 1; }
done
# Fail before authentication or build if a packaged runtime resource is unavailable.
python3 - "$repository_root/src/the_bot_radio/resources" <<'PY'
import sys
from pathlib import Path

resources_directory = Path(sys.argv[1])
if resources_directory.is_symlink():
    raise SystemExit("Generator resources directory must not be a symlink")
for filename in ("personas.json", "shared_catalog.jsonl"):
    input_path = resources_directory / filename
    if input_path.is_symlink() or not input_path.is_file() or input_path.stat().st_size == 0:
        raise SystemExit(f"Generator packaged resource must be a nonempty regular file: {filename}")
PY
work_dir="$(mktemp -d "${TMPDIR:-/tmp}/the-bot-radio-image.XXXXXXXX")"
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
repository_url="$(output_value generation_repository_url)"
aws_region="$(output_value aws_region)"
[[ "$aws_region" == us-east-1 && "$repository_url" =~ ^[0-9]{12}\.dkr\.ecr\.us-east-1\.amazonaws\.com/[a-z0-9/_-]+$ ]] || {
  echo 'Invalid generator repository or region Terraform output.' >&2; exit 1;
}
registry_url="${repository_url%%/*}"
repository_name="${repository_url#*/}"
git_commit="$(git -C "$repository_root" rev-parse --verify HEAD)"
[[ "$git_commit" =~ ^[0-9a-f]{40,64}$ ]] || { echo 'Invalid Git commit.' >&2; exit 1; }
release_uuid="$(python3 -c 'import uuid; print(uuid.uuid4().hex)')"
release_tag="git-${git_commit}-${release_uuid}"
image_uri="$repository_url:$release_tag"
aws ecr get-login-password --region "$aws_region" \
  | docker login --username AWS --password-stdin "$registry_url" >&2
docker buildx build --platform linux/amd64 --load \
  --file "$repository_root/deploy/Dockerfile.generator" --tag "$image_uri" "$repository_root" >&2
# Explicit arguments replace CMD; neither smoke command starts generation.
docker run --rm --platform linux/amd64 --network none "$image_uri" --help >&2
docker run --rm --platform linux/amd64 --network none --interactive \
  --entrypoint python "$image_uri" - < "$repository_root/deploy/smoke-generator.py" >&2
docker push "$image_uri" >&2
image_digest="$(aws ecr describe-images --region "$aws_region" \
  --repository-name "$repository_name" --image-ids "imageTag=$release_tag" \
  --query 'imageDetails[0].imageDigest' --output text)"
[[ "$image_digest" =~ ^sha256:[0-9a-f]{64}$ ]] || { echo 'Invalid ECR digest.' >&2; exit 1; }
printf '%s@%s\n' "$repository_url" "$image_digest"
