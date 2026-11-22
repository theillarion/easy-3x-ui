#!/bin/sh
set -eu

if [ "$(id -u)" -ne 0 ]; then
    echo "Run this script with sudo." >&2
    exit 1
fi

. /etc/os-release
case "${ID:-}:${VERSION_ID:-}" in
    debian:12|debian:13) ;;
    *) echo "This installer needs Debian 12 or 13." >&2; exit 1 ;;
esac

if ! command -v python3 >/dev/null || ! python3 -c 'import yaml' 2>/dev/null; then
    apt-get update
    apt-get install -y --no-install-recommends python3 python3-yaml
fi

here=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
case "$0" in
    install.sh|*/install.sh)
        if [ -f "$here/setup_3x_ui.py" ] && [ -f "$here/setup3ui/cli.py" ]; then
            SETUP_3X_UI_DEPLOY_CLI=1 exec python3 "$here/setup_3x_ui.py" "$@"
        fi
        ;;
esac

stage=$(mktemp -d)
trap 'rm -r -- "$stage"' EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
source_url=${SETUP_3X_UI_SOURCE_URL:-https://codeload.github.com/theillarion/easy-3x-ui/tar.gz/refs/heads/master}

curl -fsSL --proto '=https' --proto-redir '=https' --retry 3 --connect-timeout 10 --max-time 120 \
    "$source_url" -o "$stage/source.tar.gz"
mkdir "$stage/source"
tar -xzf "$stage/source.tar.gz" -C "$stage/source" --strip-components=1 --no-same-owner --no-same-permissions
if [ ! -f "$stage/source/setup_3x_ui.py" ] || [ ! -f "$stage/source/setup3ui/cli.py" ]; then
    echo "The downloaded archive is missing setup-3x-ui." >&2
    exit 1
fi
SETUP_3X_UI_DEPLOY_CLI=1 python3 "$stage/source/setup_3x_ui.py" "$@"
