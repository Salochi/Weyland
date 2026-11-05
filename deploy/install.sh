#!/usr/bin/env bash
# Install/upgrade Weyland on the target box. Runs as root inside the CT.
# Expects the source tarball on stdin's sibling path /tmp/weyland-src.tgz.
set -euo pipefail
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

SRC=/tmp/weyland-src.tgz
APP=/opt/weyland
ALLOWED_USERS="${1:?usage: install.sh <tailnet-login>}"

# Running straight from a git clone: if the deploy tarball isn't present,
# package this checkout ourselves (same exclusions as the deploy recipe).
if [ ! -f "$SRC" ]; then
  repo="$(cd "$(dirname "$0")/.." && pwd)"
  [ -f "$repo/pyproject.toml" ] || { echo "neither $SRC nor a Weyland checkout at $repo" >&2; exit 1; }
  tar -C "$repo" --exclude=.venv --exclude=.git --exclude=__pycache__ --exclude=.pytest_cache --exclude=.hermes -czf "$SRC" .
fi

id weyland >/dev/null 2>&1 || useradd --system --home-dir "$APP" --shell /usr/sbin/nologin weyland
install -d -m 0750 -o root -g weyland /etc/weyland
install -d -m 0750 -o weyland -g weyland /var/lib/weyland
install -d -m 0755 -o root -g root "$APP"

# uv for a self-contained venv (Ubuntu python3 lacks ensurepip).
if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh >/dev/null
fi

rm -rf "$APP/src.new" && mkdir -p "$APP/src.new"
tar -xzf "$SRC" -C "$APP/src.new"
rm -rf "$APP/src" && mv "$APP/src.new" "$APP/src"
chown -R root:root "$APP/src"

UV_PYTHON_INSTALL_DIR=/opt/weyland/python uv venv -q --allow-existing "$APP/.venv"
VIRTUAL_ENV="$APP/.venv" uv pip install -q --python "$APP/.venv/bin/python" "$APP/src"

[ -f /etc/weyland/weyland.env ] || install -m 0640 -o root -g weyland "$APP/src/deploy/weyland.env.example" /etc/weyland/weyland.env
sed -i "s/^WEYLAND_ALLOWED_USERS=.*/WEYLAND_ALLOWED_USERS=${ALLOWED_USERS}/" /etc/weyland/weyland.env

# Token key: root-owned, readable by the service group only.
if [ ! -f /etc/weyland/token.key ]; then
  "$APP/.venv/bin/python" -c "from weyland.crypto import create_key; from pathlib import Path; create_key(Path('/etc/weyland/token.key'))"
fi
chown root:weyland /etc/weyland/token.key && chmod 0640 /etc/weyland/token.key

install -m 0644 "$APP/src/deploy/weyland-web.service" /etc/systemd/system/
install -m 0644 "$APP/src/deploy/weyland-sync.service" /etc/systemd/system/
install -m 0644 "$APP/src/deploy/weyland-sync.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl enable -q weyland-web.service weyland-sync.timer
systemctl restart weyland-web.service
systemctl start weyland-sync.timer
rm -f "$SRC"
echo "installed $(sudo -u weyland "$APP/.venv/bin/python" -c 'import weyland; print(weyland.__version__)')"
