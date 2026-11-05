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
STAGE=/srv/kanjikyoushi/$FILE  # on disk: the chroot's /tmp is a 1.7 GB tmpfs, and the database is 6 GB

mkdir -p "$DEST"
if $USB; then
    adb shell "su -c 'sh /data/debian/srv/kanjikyoushi/phone/chroot.sh run sqlite3 $DB \".backup $STAGE\" || { rm -f /data/debian$STAGE; exit 1; }'"
    adb shell "su -c 'mv /data/debian$STAGE /data/local/tmp/$FILE && chmod 644 /data/local/tmp/$FILE'"
    adb pull "/data/local/tmp/$FILE" "$DEST/$FILE" >/dev/null
    adb shell "rm /data/local/tmp/$FILE"
else
    ssh kanjikyoushi "sqlite3 $DB '.backup $STAGE' || { rm -f $STAGE; exit 1; }"
    scp -q "kanjikyoushi:$STAGE" "$DEST/$FILE"
    ssh kanjikyoushi "rm $STAGE"
fi
echo "Saved $DEST/$FILE ($(du -h "$DEST/$FILE" | cut -f1))"
