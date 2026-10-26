#!/usr/bin/env bash
# Build the backend and frontend on this Mac and install them on the phone.
# Goes over SSH on the home Wi-Fi (`ssh kanjikyoushi`, set up by setup.sh); pass --usb to use adb over the cable.
# Expects the frontend checked out next to the backend (../frontend/client), as in kanjikyoushi/.
# The phone's database is never replaced: the first deploy copies yours there, later ones only migrate it.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
BACKEND=$(cd "$HERE/../.." && pwd)
FRONTEND=$(cd "$BACKEND/../frontend/client" && pwd)
APP=/srv/kanjikyoushi  # inside Debian; /data/debian/srv/kanjikyoushi from Android
STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT

if [ "${1:-}" = --usb ]; then
    # put FILE DEST: copy a file to a path inside Debian. on_phone CMD: run a shell command inside Debian.
    put() { adb push "$1" /data/local/tmp/kanjikyoushi.tmp >/dev/null && adb shell "su -c 'mv /data/local/tmp/kanjikyoushi.tmp /data/debian$2'"; }
    on_phone() { adb shell "su -c 'sh /data/debian$APP/phone/chroot.sh run bash -c \"$*\"'"; }
else
    ssh -q -o ConnectTimeout=5 kanjikyoushi true || {
        echo "Can't reach the phone over SSH (are you on the home Wi-Fi?). With the cable plugged in, use --usb." >&2; exit 1; }
    put() { scp -q "$1" "kanjikyoushi:$2"; }
    on_phone() { ssh kanjikyoushi "$*"; }
fi

echo "Building the frontend..."
(cd "$FRONTEND" && NEXT_PUBLIC_HOST=/api npm run -s build >/dev/null)
mkdir -p "$STAGE/frontend/.next" "$STAGE/phone"
cp -R "$FRONTEND/.next/standalone/." "$STAGE/frontend/"
cp -R "$FRONTEND/.next/static" "$STAGE/frontend/.next/static"
[ ! -d "$FRONTEND/public" ] || cp -R "$FRONTEND/public" "$STAGE/frontend/public"

echo "Packing the backend..."
rsync -a --exclude .venv --exclude .git --exclude __pycache__ --exclude 'server/db.sqlite3*' \
    --exclude /data/ --exclude /deploy/ "$BACKEND/" "$STAGE/backend/"
cp "$HERE"/*.sh "$STAGE/phone/"
COPYFILE_DISABLE=1 tar --no-xattrs --no-mac-metadata -czf "$STAGE/release.tgz" -C "$STAGE" backend frontend phone

if ! on_phone "test -f $APP/data/db.sqlite3"; then
    echo "First deploy: copying your database to the phone..."
    sqlite3 "$BACKEND/server/db.sqlite3" ".backup '$STAGE/db.sqlite3'"
    put "$STAGE/db.sqlite3" "$APP/data/db.sqlite3"
    on_phone "chown 2900:2900 $APP/data/db.sqlite3 && chmod 600 $APP/data/db.sqlite3"
fi

echo "Installing on the phone..."
put "$STAGE/release.tgz" "$APP/release.tgz"
on_phone "$APP/phone/release.sh $APP/release.tgz"

sleep 6
code=$(on_phone "curl -s -o /dev/null -w %{http_code} http://127.0.0.1:3000/login")
echo "Deployed. The login page answers $code on the phone."
