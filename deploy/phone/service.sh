#!/system/bin/sh
# Installed as /data/adb/service.d/kanjikyoushi.sh. Magisk runs it at every boot, in the background.
until [ "$(getprop sys.boot_completed)" = 1 ]; do sleep 5; done

# Without a wake lock the phone goes into deep sleep with the screen off and stops answering.
echo kanjikyoushi > /sys/power/wake_lock

# Outgoing: the app user (kanji, uid 2900) may answer connections made to it, but never start one except over
# loopback (Next.js to Django, Tailscale to Next.js). If the app is ever broken into, it can't reach other devices
# on the Wi-Fi or the internet.
for ipt in iptables ip6tables; do
    $ipt -w -C oem_out -m owner --uid-owner 2900 -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN 2>/dev/null ||
        $ipt -w -I oem_out -m owner --uid-owner 2900 -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN
    $ipt -w -C oem_out -m owner --uid-owner 2900 ! -o lo -j REJECT 2>/dev/null ||
        $ipt -w -A oem_out -m owner --uid-owner 2900 ! -o lo -j REJECT
done

# Incoming: the site (3000) and SSH (2222) answer loopback (Tailscale) and private home-network addresses only,
# never the internet. Both listen on IPv4 only; IPv6 to them is refused outright, since the phone may have a
# public IPv6 address.
if iptables -w -N kanjikyoushi_in 2>/dev/null; then
    iptables -w -A kanjikyoushi_in -i lo -j RETURN
    for net in 192.168.0.0/16 10.0.0.0/8 172.16.0.0/12; do iptables -w -A kanjikyoushi_in -s $net -j RETURN; done
    iptables -w -A kanjikyoushi_in -j REJECT
fi
for port in 3000 2222; do
    iptables -w -C INPUT -p tcp --dport $port -j kanjikyoushi_in 2>/dev/null ||
        iptables -w -I INPUT -p tcp --dport $port -j kanjikyoushi_in
    ip6tables -w -C INPUT -p tcp --dport $port ! -i lo -j REJECT 2>/dev/null ||
        ip6tables -w -I INPUT -p tcp --dport $port ! -i lo -j REJECT
done

sh /data/debian/srv/kanjikyoushi/phone/chroot.sh start
