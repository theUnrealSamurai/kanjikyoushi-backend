#!/system/bin/sh
# Installed as /data/adb/service.d/kanjikyoushi.sh. Magisk runs it at every boot, in the background.
until [ "$(getprop sys.boot_completed)" = 1 ]; do sleep 5; done

# Without a wake lock the phone goes into deep sleep with the screen off and stops answering.
echo kanjikyoushi > /sys/power/wake_lock

# The app user (kanji, uid 2900) may only talk to the phone itself: Next.js to Django and Tailscale to Next.js all
# happen over loopback. If the app is ever broken into, it can't reach other devices on the Wi-Fi or the internet.
for ipt in iptables ip6tables; do
    $ipt -w -C oem_out -m owner --uid-owner 2900 ! -o lo -j REJECT 2>/dev/null ||
        $ipt -w -A oem_out -m owner --uid-owner 2900 ! -o lo -j REJECT
done

sh /data/debian/srv/kanjikyoushi/phone/chroot.sh start
