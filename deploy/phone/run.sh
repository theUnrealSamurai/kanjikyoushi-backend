#!/bin/bash
# Runs inside Debian. A small supervisor that keeps sshd, tailscaled, Django (gunicorn) and Next.js running,
# restarting whichever one exits. Logs go to /srv/kanjikyoushi/logs/.
#
#   run.sh start         start the supervisor in the background (boot calls this)
#   run.sh stop          stop the supervisor and everything it started
#   run.sh restart-app   restart Django and Next.js after a deploy; tailscaled keeps running
set -u
APP=/srv/kanjikyoushi
PIDFILE=/run/kanjikyoushi.pid

running() { [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; }

as_app() { setpriv --reuid=kanji --regid=kanji --init-groups env HOME=/home/kanji "$@"; }

keep() {  # NAME CMD...: run CMD forever, appending its output to logs/NAME.log
    local name=$1; shift
    (
        while :; do
            "$@" >>"$APP/logs/$name.log" 2>&1
            echo "$(date -Is) $name exited with $?, restarting in 2s" >>"$APP/logs/$name.log"
            sleep 2
        done
    ) &
}

trim_logs() {  # keep the last 1 MB of any log over 10 MB; rewrite in place so open handles keep working
    for log in "$APP"/logs/*.log; do
        [ -f "$log" ] && [ "$(stat -c %s "$log")" -gt 10485760 ] || continue
        tail -c 1048576 "$log" > "$log.tmp" && cat "$log.tmp" > "$log" && rm "$log.tmp"
    done
}

supervise() {
    echo $$ > "$PIDFILE"
    echo -900 > /proc/self/oom_score_adj  # Android's low-memory killer takes apps long before these
    set -a; . "$APP/env"; set +a
    mkdir -p /run/tailscale /run/sshd

    keep sshd /usr/sbin/sshd -D -e
    keep tailscaled tailscaled --tun=userspace-networking --statedir=/var/lib/tailscale \
        --socket=/run/tailscale/tailscaled.sock
    # The access log records X-Forwarded-For, the only place the real client address shows up behind the proxies.
    keep backend as_app "$APP/venv/bin/gunicorn" --chdir "$APP/backend/server" server.wsgi \
        --bind 127.0.0.1:8000 --workers 2 --worker-tmp-dir /tmp \
        --access-logfile - --access-logformat '%(t)s %({x-forwarded-for}i)s "%(r)s" %(s)s %(L)ss'
    # Reachable through Tailscale (over loopback) and from the home Wi-Fi; service.sh's firewall refuses the rest.
    keep frontend as_app env PORT=3000 HOSTNAME=0.0.0.0 node "$APP/frontend/server.js"

    while :; do sleep 3600; trim_logs; done
}

case "${1:-}" in
    start)
        running && { echo "already running"; exit 0; }
        setsid "$0" supervise </dev/null >/dev/null 2>&1 &
        echo "started"
        ;;
    supervise) supervise ;;
    stop)
        running || { echo "not running"; exit 0; }
        kill -- "-$(cat "$PIDFILE")" && rm -f "$PIDFILE" && echo "stopped"
        ;;
    restart-app)
        running || exec "$0" start
        # Next.js renames its process to "next-server (vX.Y.Z)", so its original command line no longer matches.
        pkill -f "gunicorn .*server.wsgi"; pkill -f "node $APP/frontend/server.js"; pkill -f "^next-server"
        echo "restarting Django and Next.js"
        ;;
    *) echo "usage: run.sh start | stop | restart-app"; exit 2 ;;
esac
