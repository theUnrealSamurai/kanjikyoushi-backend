#!/bin/bash
# Runs inside Debian: Django's manage.py as the app user, with the server's settings.
# From the Mac: ssh kanjikyoushi /srv/kanjikyoushi/phone/manage.sh invite --list
set -a; . /srv/kanjikyoushi/env; set +a
exec setpriv --reuid=kanji --regid=kanji --init-groups /srv/kanjikyoushi/venv/bin/python \
    /srv/kanjikyoushi/backend/server/manage.py "$@"
