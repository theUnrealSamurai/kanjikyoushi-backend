#!/usr/bin/env bash
# One-time setup of the phone over adb: unpack Debian into /data/debian, run bootstrap.sh inside it and install
# the Magisk boot script. Needs the phone rooted with Magisk and adb's Shell granted root. Safe to rerun.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
CACHE=${XDG_CACHE_HOME:-$HOME/.cache}/kanjikyoushi-phone
APP=/data/debian/srv/kanjikyoushi
TMP=/data/local/tmp/kanjikyoushi

root() { adb shell "su -c '$*'"; }

[ "$(adb shell su -c id 2>/dev/null | cut -d' ' -f1)" = "uid=0(root)" ] || {
    echo "adb has no root on the phone: grant Shell in Magisk > Superuser first." >&2; exit 1; }

mkdir -p "$CACHE"
[ -f "$CACHE/debian.tar.gz" ] || uv run --no-project python -I "$HERE/fetch_debian.py" "$CACHE/debian.tar.gz"

adb shell "rm -rf $TMP && mkdir -p $TMP"
adb push "$HERE"/*.sh "$TMP/" >/dev/null
if ! root "test -x /data/debian/bin/bash"; then
    adb push "$CACHE/debian.tar.gz" "$TMP/debian.tar.gz" >/dev/null
    root "sh $TMP/chroot.sh install $TMP/debian.tar.gz"
fi

root "mkdir -p $APP/phone && cp $TMP/*.sh $APP/phone/ && chmod 755 $APP/phone/*.sh"
root "sh $APP/phone/chroot.sh run /srv/kanjikyoushi/phone/bootstrap.sh"
root "cp $APP/phone/service.sh /data/adb/service.d/kanjikyoushi.sh && chmod 755 /data/adb/service.d/kanjikyoushi.sh"
root "cp $APP/phone/battery.sh /data/adb/service.d/kanjikyoushi-battery.sh && chmod 755 /data/adb/service.d/kanjikyoushi-battery.sh"

# SSH from this Mac: a key used only for the phone, and an `ssh kanjikyoushi` shortcut pointing at its Wi-Fi address.
KEY=$HOME/.ssh/kanjikyoushi_ed25519
[ -f "$KEY" ] || ssh-keygen -q -t ed25519 -N "" -C "kanjikyoushi deploy key" -f "$KEY"
adb push "$KEY.pub" "$TMP/key.pub" >/dev/null
BLOB=$(cut -d' ' -f2 "$KEY.pub")
root "grep -q $BLOB /data/debian/root/.ssh/authorized_keys 2>/dev/null || cat $TMP/key.pub >> /data/debian/root/.ssh/authorized_keys; chmod 600 /data/debian/root/.ssh/authorized_keys"
PHONE_IP=$(adb shell ip -4 addr show wlan0 | awk '/inet /{sub("/.*", "", $2); print $2}')
if ! grep -q "^Host kanjikyoushi$" "$HOME/.ssh/config" 2>/dev/null; then
    cat >> "$HOME/.ssh/config" <<EOF

Host kanjikyoushi
    HostName $PHONE_IP
    Port 2222
    User root
    IdentityFile $KEY
    IdentitiesOnly yes
    StrictHostKeyChecking accept-new
EOF
    echo "Added 'Host kanjikyoushi' ($PHONE_IP) to ~/.ssh/config"
fi

adb shell "rm -rf $TMP"
echo "Phone is set up. Next: deploy.sh"
