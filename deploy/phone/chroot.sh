#!/system/bin/sh
# The Debian chroot that the server runs in, from the Android side. Run as root (adb shell su -c "sh chroot.sh ...").
#
#   chroot.sh install TARBALL   unpack a Debian rootfs into /data/debian (first setup only)
#   chroot.sh start             mount and start the server (what boot does)
#   chroot.sh stop              stop the server
#   chroot.sh shell             a root shell inside Debian
#   chroot.sh run CMD...        run one command inside Debian
R=/data/debian
BB=/data/adb/magisk/busybox

mount_all() {
    grep -q " $R/proc " /proc/mounts && return
    $BB mount -o rbind /dev "$R/dev"
    $BB mount -t proc proc "$R/proc"
    $BB mount -t sysfs sysfs "$R/sys"
    $BB mount -t tmpfs -o mode=1777 tmpfs "$R/tmp"
    $BB mount -t tmpfs -o mode=755 tmpfs "$R/run"
}

in_debian() {
    mount_all
    $BB chroot "$R" /usr/bin/env -i HOME=/root TERM="${TERM:-xterm}" LANG=C.UTF-8 \
        PATH=/opt/node/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin "$@"
}

case "$1" in
    install)
        [ -x "$R/bin/bash" ] && { echo "Debian is already in $R"; exit 0; }
        mkdir -p "$R" && $BB tar -xzf "$2" -C "$R" && echo "Debian unpacked into $R"
        ;;
    start) in_debian /srv/kanjikyoushi/phone/run.sh start ;;
    stop) in_debian /srv/kanjikyoushi/phone/run.sh stop ;;
    shell) in_debian /bin/bash -l ;;
    run) shift; in_debian "$@" ;;
    *) echo "usage: chroot.sh install TARBALL | start | stop | shell | run CMD..."; exit 2 ;;
esac
