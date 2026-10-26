#!/system/bin/sh
# Installed as /data/adb/service.d/kanjikyoushi-battery.sh. Keeps the always-plugged-in battery between MIN and
# MAX percent instead of at 100%. Uses Xiaomi's charge restriction (qcom-battery), which pauses charging while USB
# stays online, so Android sees no unplug. While paused the battery carries the load (~150 mA idle, ~1 day 80->40%).
MIN=${MIN:-40}; MAX=${MAX:-80}; SLEEP=${SLEEP:-180}
Q=/sys/class/qcom-battery
CAP=/sys/class/power_supply/battery/capacity
LOG=/data/adb/kanjikyoushi-battery.log
PIDF=/dev/kanjikyoushi-battery.pid  # /dev is cleared at boot, so a stale pid can't block the next start

[ -f $PIDF ] && kill -0 "$(cat $PIDF)" 2>/dev/null && exit 0
echo $$ > $PIDF

while [ "$(getprop sys.boot_completed)" != 1 ] || [ ! -w $Q/restrict_chg ]; do sleep 5; done

CUR=$(cat $Q/restrict_cur 2>/dev/null)
case $CUR in ''|0|*[!0-9]*) CUR=1000000;; esac

pause()  { echo 0 > $Q/restrict_cur; echo 1 > $Q/restrict_chg; echo "$(date '+%F %T') pause at $c%" >> $LOG; }
resume() { echo 0 > $Q/restrict_chg; echo $CUR > $Q/restrict_cur; echo "$(date '+%F %T') resume at $c%" >> $LOG; }
trap 'c=exit; resume; rm -f $PIDF; exit 0' TERM INT HUP

while :; do
    c=$(cat $CAP 2>/dev/null)
    case $c in
        ''|*[!0-9]*) ;;  # bad read: change nothing
        *)
            s=$(cat $Q/restrict_chg 2>/dev/null)
            if [ "$c" -ge "$MAX" ] && [ "$s" != 1 ]; then pause
            elif [ "$c" -le "$MIN" ] && [ "$s" != 0 ]; then resume
            fi
            ;;
    esac
    sleep $SLEEP & wait $!
done
