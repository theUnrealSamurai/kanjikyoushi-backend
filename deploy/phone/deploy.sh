#!/usr/bin/env bash
# Build the backend and frontend on this Mac and install them on the phone over adb.
# Expects the frontend checked out next to the backend (../frontend/client), as in kanjikyoushi/.
# The phone's database is never replaced: the first deploy copies yours there, later ones only migrate it.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
BACKEND=$(cd "$HERE/../.." && pwd)
FRONTEND=$(cd "$BACKEND/../frontend/client" && pwd)
APP=/data/debian/srv/kanjikyoushi
STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT

root() { adb shell "su -c '$*'"; }

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

if ! root "test -f $APP/data/db.sqlite3"; then
    echo "First deploy: copying your database to the phone..."
    sqlite3 "$BACKEND/server/db.sqlite3" ".backup '$STAGE/db.sqlite3'"
    adb push "$STAGE/db.sqlite3" /data/local/tmp/kanjikyoushi-db.sqlite3
    root "mv /data/local/tmp/kanjikyoushi-db.sqlite3 $APP/data/db.sqlite3 && chown 2900:2900 $APP/data/db.sqlite3 && chmod 600 $APP/data/db.sqlite3"
fi

echo "Installing on the phone..."
adb push "$STAGE/release.tgz" /data/local/tmp/kanjikyoushi-release.tgz >/dev/null
root "mv /data/local/tmp/kanjikyoushi-release.tgz $APP/release.tgz"
root "sh $APP/phone/chroot.sh run /srv/kanjikyoushi/phone/release.sh /srv/kanjikyoushi/release.tgz"

sleep 6
code=$(root "sh $APP/phone/chroot.sh run curl -s -o /dev/null -w %{http_code} http://127.0.0.1:3000/login")
echo "Deployed. The login page answers $code on the phone."
