#!/bin/bash
# Runs inside Debian. Installs a release made by deploy.sh: Python packages, database migrations, then the
# new code, and restarts the app. The database in data/ is only ever migrated, never replaced.
set -euo pipefail
APP=/srv/kanjikyoushi
NEW=$APP/release.new

[ -f "$APP/data/db.sqlite3" ] || { echo "No database at $APP/data/db.sqlite3. deploy.sh copies yours on the first deploy." >&2; exit 1; }

rm -rf "$NEW" && mkdir -p "$NEW"
tar -xzf "$1" -C "$NEW" --no-same-owner
rm -f "$1"

# One shared virtualenv outside the code folder, so swapping the code never breaks it.
# The corpus tools (fugashi, cutlet, the UniDic dictionary) only run on the Mac, and Postgres isn't used.
export UV_PROJECT_ENVIRONMENT=$APP/venv UV_PYTHON_INSTALL_DIR=/opt/uv-python UV_CACHE_DIR=/var/cache/uv
(cd "$NEW/backend" && uv sync --frozen --no-dev --quiet \
    --no-install-package fugashi --no-install-package cutlet --no-install-package unidic-lite \
    --no-install-package mojimoji --no-install-package jaconv \
    --no-install-package psycopg --no-install-package psycopg-binary)

set -a; . "$APP/env"; set +a
setpriv --reuid=kanji --regid=kanji --init-groups "$APP/venv/bin/python" "$NEW/backend/server/manage.py" migrate --noinput

for part in backend frontend; do
    rm -rf "$APP/$part.old"
    [ -d "$APP/$part" ] && mv "$APP/$part" "$APP/$part.old"
    mv "$NEW/$part" "$APP/$part"
done
cp "$NEW"/phone/*.sh "$APP/phone/" && chmod 755 "$APP"/phone/*.sh
rm -rf "$NEW"

"$APP/phone/run.sh" restart-app
