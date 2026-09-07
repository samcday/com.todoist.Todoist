#!/usr/bin/env bash
# Build and test the personal ARM64 repository on a disposable native CI runner.
# SIGNING_HOME must contain the private key corresponding to packaging/repo.gpg.
set -euo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
project_dir=$(cd -- "$script_dir/.." && pwd)
cd -- "$project_dir"

app_id=com.todoist.Todoist
app_ref="app/$app_id/aarch64/master"
locale_ref="runtime/$app_id.Locale/aarch64/master"
expected_fingerprint=299958F6C6EC614C841D059030C81C62E1AF5BA2
publish_url=${PUBLISH_URL:-https://samcday.github.io/com.todoist.Todoist}
publish_url=${publish_url%/}
bootstrap=${BOOTSTRAP:-false}
work_dir="$project_dir/.work"
build_repo="$work_dir/buildrepo"
site_dir="$work_dir/site"
publish_repo="$site_dir/repo"
public_key="$project_dir/packaging/repo.gpg"

fail() {
    printf 'Repository build failed: %s\n' "$*" >&2
    exit 1
}

[[ $(uname -m) == aarch64 ]] || fail 'a native aarch64 runner is required'
[[ $(flatpak --default-arch) == aarch64 ]] || fail 'Flatpak must use aarch64'
[[ $bootstrap == true || $bootstrap == false ]] || fail 'BOOTSTRAP must be true or false'
[[ $publish_url == https://* && $publish_url != *$'\n'* && $publish_url != *$'\r'* ]] ||
    fail 'PUBLISH_URL must be a single HTTPS URL'
[[ -n ${SIGNING_HOME:-} && -d $SIGNING_HOME ]] || fail 'SIGNING_HOME must contain the imported signing key'
[[ -f $public_key ]] || fail 'packaging/repo.gpg is missing'

mkdir -p -- "$work_dir"
if [[ -z ${XDG_RUNTIME_DIR:-} ]]; then
    export XDG_RUNTIME_DIR="$work_dir/runtime"
    mkdir -p -- "$XDG_RUNTIME_DIR"
    chmod 700 -- "$XDG_RUNTIME_DIR"
fi
if [[ -z ${DBUS_SESSION_BUS_ADDRESS:-} ]]; then
    exec dbus-run-session -- "$script_dir/build-repository.sh" "$@"
fi

public_fingerprint=$(
    gpg --homedir "$SIGNING_HOME" --batch --with-colons --show-keys "$public_key" |
        awk -F: '$1 == "fpr" && !seen++ { print $10 }'
)
[[ $public_fingerprint == "$expected_fingerprint" ]] || fail 'public repository key fingerprint changed'
gpg --homedir "$SIGNING_HOME" --batch --list-secret-keys "$expected_fingerprint" >/dev/null

# These are disposable build outputs. Keep checker results and diagnostic logs.
rm -rf -- "$build_repo" "$site_dir"
mkdir -p -- "$site_dir"
ostree --repo="$publish_repo" init --mode=archive-z2

# Never treat a failed download or signature check as permission to erase history.
http_status=$(
    curl --silent --show-error --location --proto '=https' --proto-redir '=https' \
        --retry 3 --retry-delay 2 --connect-timeout 15 --max-time 60 \
        --output /dev/null --write-out '%{http_code}' "$publish_url/repo/summary"
)
case "$http_status" in
    200)
        ostree --repo="$publish_repo" remote add --gpg-import="$public_key" \
            --set=gpg-verify-summary=true published "$publish_url/repo"
        ostree --repo="$publish_repo" pull --mirror --depth=2 published
        ostree --repo="$publish_repo" remote delete published
        ;;
    404)
        [[ $bootstrap == true ]] || fail 'published repository is absent; explicit BOOTSTRAP=true is required'
        printf 'Bootstrapping the first repository after a confirmed HTTP 404.\n'
        ;;
    *) fail "published repository returned HTTP $http_status" ;;
esac

flatpak remote-add --user --if-not-exists flathub \
    https://dl.flathub.org/repo/flathub.flatpakrepo
flatpak-builder --user --arch=aarch64 --default-branch=master \
    --install-deps-from=flathub --assumeyes --disable-rofiles-fuse \
    --force-clean --rebuild-on-sdk-change --repo="$build_repo" \
    --state-dir="$work_dir/builder-state" "$work_dir/app" com.todoist.Todoist.yaml

# Import only the app and its optional locale extension. Signing belongs to the
# publishing repository, whose parents are the previous successful deployment.
build_refs=$(ostree --repo="$build_repo" refs)
printf '%s\n' "$build_refs" | grep -Fxq "$app_ref" || fail 'builder did not export the expected app ref'
publish_refs=("$app_ref")
if printf '%s\n' "$build_refs" | grep -Fxq "$locale_ref"; then
    publish_refs+=("$locale_ref")
fi
flatpak build-commit-from --src-repo="$build_repo" --gpg-sign="$expected_fingerprint" \
    --gpg-homedir="$SIGNING_HOME" --timestamp=NOW --no-update-summary \
    "$publish_repo" "${publish_refs[@]}"
flatpak build-update-repo --title="Sam's Todoist" --default-branch=master \
    --gpg-import="$public_key" --gpg-sign="$expected_fingerprint" \
    --gpg-homedir="$SIGNING_HOME" --generate-static-deltas --prune --prune-depth=2 \
    "$publish_repo"

# Install from a real signed repository: bundles do not exercise extra-data
# installation correctly. This downloads Doist's payload and runs apply_extra.
test_remote="todoist-ci-${GITHUB_RUN_ID:-$$}"
flatpak remote-add --user --no-enumerate --gpg-import="$public_key" \
    "$test_remote" "file://$publish_repo"
flatpak install --user --noninteractive --assumeyes "$test_remote" "$app_ref"
installed_dir=$(flatpak info --user --show-location "$app_ref")
python3 - "$installed_dir/files/extra/todoist/app/todoist" <<'PY'
from pathlib import Path
import struct
import sys

with Path(sys.argv[1]).open("rb") as executable:
    header = executable.read(64)
if len(header) != 64 or header[:6] != b"\x7fELF\x02\x01" or struct.unpack_from("<H", header, 18)[0] != 183:
    raise SystemExit("Installed Todoist executable is not little-endian ELF64 AArch64")
print("Installed executable is native ELF64 AArch64.")
PY
flatpak run --user --unshare=network --no-session-bus --no-a11y-bus \
    --no-documents-portal --command=sh "$app_ref" -c '
        set -eu
        ldd /app/extra/todoist/app/todoist > /tmp/todoist-ldd.txt
        cat /tmp/todoist-ldd.txt
        if grep -q "not found" /tmp/todoist-ldd.txt; then
            exit 1
        fi
    ' | tee "$work_dir/ldd.log"

# No account or web service is needed to verify creation of the desktop window.
# A timeout is success only when the app remained running and opened a window.
cleanup_smoke() {
    flatpak kill "$app_id" >/dev/null 2>&1 || true
}
trap cleanup_smoke EXIT
smoke_status=0
timeout --signal=TERM --kill-after=5s 35s \
    xvfb-run --auto-servernum --server-args='-screen 0 1280x800x24 -nolisten tcp' \
    flatpak run --user --unshare=network --nosocket=wayland --socket=x11 \
        --env=XDG_SESSION_TYPE=x11 --env=ELECTRON_ENABLE_LOGGING=1 \
        "$app_ref" --disable-gpu --ozone-platform=x11 \
        --user-data-dir=/tmp/todoist-ci-profile >"$work_dir/launch.log" 2>&1 || smoke_status=$?
cleanup_smoke
trap - EXIT
cat "$work_dir/launch.log"
[[ $smoke_status == 124 ]] || fail "headless launch exited unexpectedly (status $smoke_status)"
grep -Fq 'Opening new window' "$work_dir/launch.log" || fail 'headless launch did not report opening a window'

export TODOIST_PUBLISH_URL="$publish_url" TODOIST_SITE_DIR="$site_dir"
export TODOIST_WORKFLOW_COMMIT="${GITHUB_SHA:-$(git rev-parse HEAD)}"
export TODOIST_APP_COMMIT
TODOIST_APP_COMMIT=$(ostree --repo="$publish_repo" rev-parse "$app_ref")
python3 - <<'PY'
import base64
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil

import yaml

site = Path(os.environ["TODOIST_SITE_DIR"])
url = os.environ["TODOIST_PUBLISH_URL"]
manifest_bytes = Path("com.todoist.Todoist.yaml").read_bytes()
manifest = yaml.safe_load(manifest_bytes)
sources = [
    source
    for module in manifest["modules"] if isinstance(module, dict)
    for source in module.get("sources", [])
    if source.get("type") == "extra-data" and source.get("only-arches") == ["aarch64"]
]
if len(sources) != 1:
    raise SystemExit("Expected exactly one aarch64 extra-data source")
source = sources[0]
version_match = re.search(r"/Todoist-linux-([0-9]+(?:\.[0-9]+)+)-arm64-latest\.AppImage$", source["url"])
if not version_match:
    raise SystemExit("Cannot determine the official ARM64 Todoist version")
key = base64.b64encode(Path("packaging/repo.gpg").read_bytes()).decode("ascii")
(site / "todoist.flatpakrepo").write_text(
    "[Flatpak Repo]\nTitle=Sam's Todoist\n"
    f"Url={url}/repo\n"
    "Homepage=https://github.com/samcday/com.todoist.Todoist\n"
    "Comment=Personal ARM64 packaging of the official Todoist application\n"
    f"DefaultBranch=master\nGPGKey={key}\n"
)
(site / "todoist.flatpakref").write_text(
    "[Flatpak Ref]\nName=com.todoist.Todoist\nTitle=Todoist\n"
    "Branch=master\nIsRuntime=false\nSuggestRemoteName=samcday-todoist\n"
    f"Url={url}/repo\nGPGKey={key}\n"
    "RuntimeRepo=https://dl.flathub.org/repo/flathub.flatpakrepo\n"
)
shutil.copyfile("packaging/repo.gpg", site / "repo.gpg")
release = {
    "version": version_match[1],
    "runtime": manifest["runtime-version"],
    "architecture": "aarch64",
    "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
    "workflow_commit": os.environ["TODOIST_WORKFLOW_COMMIT"],
    "app_commit": os.environ["TODOIST_APP_COMMIT"],
    "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
    "extra_data_url": source["url"],
    "extra_data_sha256": source["sha256"],
}
if os.environ.get("BASE_COMMIT"):
    release["base_commit"] = os.environ["BASE_COMMIT"]
(site / "release.json").write_text(json.dumps(release, indent=2, sort_keys=True) + "\n")
print(json.dumps(release, sort_keys=True))
PY
printf 'Signed, installed, and smoke-tested repository is ready in %s\n' "$site_dir"
