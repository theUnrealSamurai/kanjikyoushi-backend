#!/bin/bash
# Runs inside Debian, detached: stopping the app also stops sshd. Puts a database built on the Mac (from a recent
# backup of this one, with a new corpus) in place of the live one, keeping every user's progress: the app stops,
# adopt_user_data copies the users' tables from the live database into the new one (and checks the word ids match),
# the files swap, and the app starts again. The live database is kept as data/db.before-DATE.sqlite3.
# If adopt_user_data fails, nothing is swapped. Output goes to logs/swap.log.
# Usage (from the Mac): ssh kanjikyoushi 'setsid /srv/kanjikyoushi/phone/swap_db.sh /srv/kanjikyoushi/data/db.new.sqlite3 </dev/null >/dev/null 2>&1 &'
set -uo pipefail
APP=/srv/kanjikyoushi
NEW=$1
LIVE=$APP/data/db.sqlite3
OLD=$APP/data/db.before-$(date +%Y%m%d-%H%M).sqlite3
exec >>"$APP/logs/swap.log" 2>&1

echo "$(date -Is) swapping in $NEW"
"$APP/phone/run.sh" stop
sleep 3
set -a; . "$APP/env"; set +a
if DJANGO_SQLITE_PATH=$NEW setpriv --reuid=kanji --regid=kanji --init-groups \
        "$APP/venv/bin/python" "$APP/backend/server/manage.py" adopt_user_data --from "$LIVE"; then
    mv "$LIVE" "$OLD" && mv "$NEW" "$LIVE" && chown kanji:kanji "$LIVE" && chmod 600 "$LIVE" \
        && echo "$(date -Is) swapped; the old database is $OLD" \
        || echo "$(date -Is) the swap failed part way: check $LIVE and $OLD by hand"
else
    echo "$(date -Is) adopt_user_data failed: the live database is unchanged"
fi
"$APP/phone/run.sh" start
echo "$(date -Is) app started"
