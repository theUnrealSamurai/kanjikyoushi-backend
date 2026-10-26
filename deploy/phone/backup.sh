#!/usr/bin/env bash
# Copy the phone's database to this Mac. Safe while the app is running (it uses SQLite's online backup).
# Usage: backup.sh [DEST_DIR]   (default ~/backups/kanjikyoushi)
set -euo pipefail
DEST=${1:-$HOME/backups/kanjikyoushi}
FILE=kanjikyoushi-$(date +%Y-%m-%d-%H%M).sqlite3
APP=/data/debian/srv/kanjikyoushi

root() { adb shell "su -c '$*'"; }

mkdir -p "$DEST"
root "sh $APP/phone/chroot.sh run sqlite3 /srv/kanjikyoushi/data/db.sqlite3 \".backup /tmp/$FILE\""
root "mv /data/debian/tmp/$FILE /data/local/tmp/$FILE && chmod 644 /data/local/tmp/$FILE"
adb pull "/data/local/tmp/$FILE" "$DEST/$FILE" >/dev/null
adb shell "rm /data/local/tmp/$FILE"
echo "Saved $DEST/$FILE ($(du -h "$DEST/$FILE" | cut -f1))"
