#!/bin/bash
# One-time setup inside the fresh Debian rootfs: base packages, the app user, Node, uv, Tailscale and the
# server's secret settings. Safe to rerun. Every download is pinned and checked against its published SHA-256.
set -euo pipefail

NODE=v24.22.1   NODE_SHA=dbabf7a1cbf947e6059ffad2c27b80041db97dea1e553b62afdee7f13fca336e
UV=0.13.5       UV_SHA=4bb69cf95ed13f23713bb7bc09f241896ea0c75b7c557e615a20604b82f9c403
TS=1.104.2      TS_SHA=5776aaa901b5ef1916e66be5bb54b9f481cd399c9c28db123a8cab620efa237b
APP=/srv/kanjikyoushi

printf 'nameserver 1.1.1.1\nnameserver 8.8.8.8\n' > /etc/resolv.conf
printf '127.0.0.1 localhost\n::1 localhost\n' > /etc/hosts
echo kanjikyoushi > /etc/hostname

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends ca-certificates curl xz-utils sqlite3 procps less tzdata \
    openssh-server openssh-sftp-server
apt-get clean

# SSH for deploys from the Mac: key-only root login on port 2222, IPv4 only. The firewall in service.sh lets
# only the home network reach it. Debian's sshd_config includes this file first, so these settings win.
cat > /etc/ssh/sshd_config.d/kanjikyoushi.conf <<'EOF'
Port 2222
ListenAddress 0.0.0.0
PermitRootLogin prohibit-password
PasswordAuthentication no
KbdInteractiveAuthentication no
UsePAM no
X11Forwarding no
AllowUsers root
EOF
ssh-keygen -A
mkdir -p -m 700 /root/.ssh

# The server runs as this user, not root.
getent group kanji >/dev/null || groupadd --system --gid 2900 kanji
groupmod --gid 2900 kanji
id kanji >/dev/null 2>&1 || useradd --system --uid 2900 --gid kanji --create-home --shell /usr/sbin/nologin kanji

fetch() {  # URL FILE SHA256
    curl -fsSL "$1" -o "$2"
    echo "$3  $2" | sha256sum -c --quiet -
}

if [ "$(/opt/node/bin/node --version 2>/dev/null)" != "$NODE" ]; then
    fetch "https://nodejs.org/dist/$NODE/node-$NODE-linux-arm64.tar.xz" /tmp/node.tar.xz "$NODE_SHA"
    rm -rf /opt/node && mkdir -p /opt/node && tar -xJf /tmp/node.tar.xz -C /opt/node --strip-components=1
fi

if [ "$(uv --version 2>/dev/null)" != "uv $UV" ]; then
    fetch "https://github.com/astral-sh/uv/releases/download/$UV/uv-aarch64-unknown-linux-gnu.tar.gz" /tmp/uv.tar.gz "$UV_SHA"
    tar -xzf /tmp/uv.tar.gz -C /usr/local/bin --strip-components=1
fi

if ! tailscale version 2>/dev/null | grep -qx "$TS"; then
    fetch "https://pkgs.tailscale.com/stable/tailscale_${TS}_arm64.tgz" /tmp/tailscale.tgz "$TS_SHA"
    tar -xzf /tmp/tailscale.tgz -C /tmp
    install -m 755 "/tmp/tailscale_${TS}_arm64/tailscale" "/tmp/tailscale_${TS}_arm64/tailscaled" /usr/local/bin/
fi
rm -rf /tmp/node.tar.xz /tmp/uv.tar.gz /tmp/tailscale.tgz "/tmp/tailscale_${TS}_arm64"
mkdir -p /var/lib/tailscale

mkdir -p "$APP/data" "$APP/logs" "$APP/phone"
chown -R kanji:kanji "$APP/data" "$APP/logs"
chmod 700 "$APP/data"
if [ ! -f "$APP/env" ]; then
    cat > "$APP/env" <<EOF
DJANGO_DEBUG=0
DJANGO_SECRET_KEY=$(head -c 48 /dev/urandom | base64 | tr -d '\n/+=')
DJANGO_ALLOWED_HOSTS=127.0.0.1,localhost
DJANGO_SQLITE_PATH=$APP/data/db.sqlite3
EOF
    chmod 600 "$APP/env"
fi
grep -q '^DJANGO_CACHE_DIR=' "$APP/env" || echo "DJANGO_CACHE_DIR=$APP/data/cache" >> "$APP/env"  # shared throttle counters
# Tailscale adds the client address to X-Forwarded-For and Next.js passes it through unchanged: one proxy.
grep -q '^DJANGO_NUM_PROXIES=' "$APP/env" || echo "DJANGO_NUM_PROXIES=1" >> "$APP/env"

echo "Bootstrap done: node $(/opt/node/bin/node --version), $(uv --version), tailscale $(tailscale version | head -1)"
