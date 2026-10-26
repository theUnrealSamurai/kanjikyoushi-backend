#!/usr/bin/env bash
# Copy the phone's database to this Mac. Safe while the app is running (it uses SQLite's online backup).
# Goes over SSH on the home Wi-Fi; pass --usb to use adb over the cable.
# Usage: backup.sh [--usb] [DEST_DIR]   (default ~/backups/kanjikyoushi)
set -euo pipefail
USB=false
[ "${1:-}" = --usb ] && { USB=true; shift; }
DEST=${1:-$HOME/backups/kanjikyoushi}
FILE=kanjikyoushi-$(date +%Y-%m-%d-%H%M).sqlite3
DB=/srv/kanjikyoushi/data/db.sqlite3

mkdir -p "$DEST"
if $USB; then
    adb shell "su -c 'sh /data/debian/srv/kanjikyoushi/phone/chroot.sh run sqlite3 $DB \".backup /tmp/$FILE\"'"
    adb shell "su -c 'mv /data/debian/tmp/$FILE /data/local/tmp/$FILE && chmod 644 /data/local/tmp/$FILE'"
    adb pull "/data/local/tmp/$FILE" "$DEST/$FILE" >/dev/null
    adb shell "rm /data/local/tmp/$FILE"
else
    ssh kanjikyoushi "sqlite3 $DB '.backup /tmp/$FILE'"
    scp -q "kanjikyoushi:/tmp/$FILE" "$DEST/$FILE"
    ssh kanjikyoushi "rm /tmp/$FILE"
fi
echo "Saved $DEST/$FILE ($(du -h "$DEST/$FILE" | cut -f1))"
