#!/usr/bin/env bash
# One-step installer for Project Tunaria Reborn on Ubuntu / Debian.
#
#   curl -fsSL https://raw.githubusercontent.com/Guinnessstache/tunaria-reborn/main/deploy/install.sh | sudo bash
#
# Installs Python and git, creates the "tunaria" service user, downloads the
# server to /opt/tunaria-reborn/server, writes /etc/tunaria-reborn.env, installs
# and starts the systemd service, and opens the firewall if ufw is active.
# Running it again updates the code and restarts the service; your settings and
# data are kept.
#
# Optional settings (put them after "sudo", e.g. "sudo PUBLIC_HOST=203.0.113.10 bash"):
#   PUBLIC_HOST   IP address players connect to (default: this server's main IP)
#   ADMIN_HOST    0.0.0.0 = admin panel reachable from the network (default),
#                 127.0.0.1 = this server only (use an SSH tunnel)
#   BRANCH        git branch to install (default: main)
#   REPO_URL      repository to install from
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/Guinnessstache/tunaria-reborn.git}"
BRANCH="${BRANCH:-main}"
ADMIN_HOST="${ADMIN_HOST:-0.0.0.0}"
SERVICE_USER="tunaria"
BASE_DIR="${BASE_DIR:-/opt/tunaria-reborn}"
APP_DIR="$BASE_DIR/server"
ENV_FILE="${ENV_FILE:-/etc/tunaria-reborn.env}"
UNIT_FILE="${UNIT_FILE:-/etc/systemd/system/tunaria-reborn.service}"
GAME_PORTS=("9735/tcp" "7000/tcp" "10070/udp" "8080/tcp")
ADMIN_PORT="8090/tcp"

step() { printf '\n==> %s\n' "$*"; }
die() { printf '\nError: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run this with sudo."
# The service user cannot read the caller's home folder; work from / instead.
cd /

step "Installing Python and git"
if command -v apt-get >/dev/null 2>&1; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y -qq python3 git >/dev/null
    apt-get install -y -qq python3-psutil >/dev/null 2>&1 || true
else
    command -v python3 >/dev/null && command -v git >/dev/null \
        || die "install python3 (3.10 or newer) and git, then run this again."
fi
python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' \
    || die "Python 3.10 or newer is required (found $(python3 --version 2>&1))."

step "Creating the $SERVICE_USER service user"
if id "$SERVICE_USER" >/dev/null 2>&1; then
    echo "already exists"
else
    useradd --system --home-dir "$BASE_DIR" --create-home --shell /usr/sbin/nologin "$SERVICE_USER"
fi
mkdir -p "$BASE_DIR"
chown "$SERVICE_USER:$SERVICE_USER" "$BASE_DIR"

as_service_user() { runuser -u "$SERVICE_USER" -- env HOME="$BASE_DIR" GIT_TERMINAL_PROMPT=0 "$@"; }

step "Downloading the server"
if [ -d "$APP_DIR/.git" ]; then
    as_service_user git -C "$APP_DIR" fetch --quiet origin "$BRANCH"
    as_service_user git -C "$APP_DIR" checkout --quiet "$BRANCH"
    as_service_user git -C "$APP_DIR" merge --quiet --ff-only "origin/$BRANCH" \
        || die "$APP_DIR has local changes or history that differs from GitHub; update it by hand."
    echo "updated to $(as_service_user git -C "$APP_DIR" log -1 --format='%h %s')"
else
    as_service_user git clone --quiet --branch "$BRANCH" "$REPO_URL" "$APP_DIR" \
        || die "could not download $REPO_URL. If the repository is private, see docs/linux-server.md (deploy key)."
    echo "installed $(as_service_user git -C "$APP_DIR" log -1 --format='%h %s')"
fi

step "Writing settings"
if [ -f "$ENV_FILE" ]; then
    echo "keeping existing $ENV_FILE"
else
    if [ -z "${PUBLIC_HOST:-}" ]; then
        PUBLIC_HOST="$( (ip -4 route get 1.1.1.1 2>/dev/null || true) | awk '{for (i = 1; i < NF; i++) if ($i == "src") {print $(i + 1); exit}}')"
        [ -n "$PUBLIC_HOST" ] || PUBLIC_HOST="$( (hostname -I 2>/dev/null || true) | awk '{print $1}')"
        [ -n "$PUBLIC_HOST" ] || PUBLIC_HOST="$(python3 -c 'import socket; s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.connect(("192.0.2.1", 9)); print(s.getsockname()[0])' 2>/dev/null || true)"
        [ -n "$PUBLIC_HOST" ] || die "could not detect this server's IP; run again with PUBLIC_HOST=<ip>."
    fi
    cat > "$ENV_FILE" <<EOF
# Options for the tunaria-reborn service (written by deploy/install.sh).
# After editing: sudo systemctl restart tunaria-reborn
# --public-host  the IP address players connect to
# --admin-host   0.0.0.0 = admin panel reachable from the network, 127.0.0.1 = this server only
TUNARIA_ARGS=--host 0.0.0.0 --public-host $PUBLIC_HOST --admin-host $ADMIN_HOST

# Optional: fixed admin password instead of a generated one.
#EQOA_ADMIN_PASSWORD=change-me
EOF
    chmod 640 "$ENV_FILE"
    chgrp "$SERVICE_USER" "$ENV_FILE"
    echo "wrote $ENV_FILE (public host $PUBLIC_HOST, admin panel on $ADMIN_HOST)"
fi

step "Installing the service"
install -m 644 "$APP_DIR/deploy/tunaria-reborn.service" "$UNIT_FILE"
systemctl daemon-reload
systemctl enable --quiet tunaria-reborn
started_at="$(date '+%Y-%m-%d %H:%M:%S')"
systemctl restart tunaria-reborn

step "Firewall"
if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "Status: active"; then
    ports=("${GAME_PORTS[@]}")
    grep -q -- "--admin-host 0.0.0.0" "$ENV_FILE" && ports+=("$ADMIN_PORT")
    for port in "${ports[@]}"; do ufw allow "$port" >/dev/null; done
    echo "opened ${ports[*]}"
else
    echo "ufw is not active; nothing to open. (Hosting at home? Forward the ports on your router.)"
fi

step "Starting"
for _ in $(seq 1 20); do
    systemctl is-active --quiet tunaria-reborn && break
    sleep 0.5
done
systemctl is-active --quiet tunaria-reborn \
    || die "the service did not start; see: journalctl -u tunaria-reborn -n 50"
sleep 2

args="$(grep '^TUNARIA_ARGS=' "$ENV_FILE" | cut -d= -f2-)"
admin_host="$(sed -n 's/.*--admin-host \([^ ]*\).*/\1/p' <<<"$args")"
public_host="$(sed -n 's/.*--public-host \([^ ]*\).*/\1/p' <<<"$args")"
if [ "${admin_host:-127.0.0.1}" = "127.0.0.1" ]; then
    admin_url="http://localhost:8090/ (from this server, or through an SSH tunnel)"
else
    admin_url="http://${public_host}:8090/"
fi
password="$(journalctl -u tunaria-reborn --since "$started_at" --no-pager 2>/dev/null | sed -n 's/.*Admin password (generated, shown once): //p' | tail -n 1)"

cat <<EOF

Project Tunaria Reborn is running.

  Admin panel:   $admin_url
EOF
if [ -n "$password" ]; then
    echo "  Admin password: $password   (you will choose your own at first sign-in)"
else
    echo "  Admin password: set earlier; forgot it? delete $APP_DIR/data/admin.json and run this again."
fi
cat <<EOF
  Game address:  $public_host  (point the game client's DNS patch here)

  Settings: $ENV_FILE    Logs: journalctl -u tunaria-reborn -f
EOF
