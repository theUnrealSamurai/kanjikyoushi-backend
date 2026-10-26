# Running KanjiKyoushi on a phone

The whole app (Django backend and Next.js frontend) runs on a rooted Xiaomi Redmi 12 5G (XIG03, Japan) at
home, inside a Debian chroot. It is private: Tailscale Serve makes it reachable only from devices in the
owner's tailnet and from the few friends the phone is shared with. There is no public URL (no Funnel). The phone
has an unlocked bootloader and Magisk. The scripts here set it up and deploy to it over adb from the Mac.

**The database holds every user's progress.** It lives at `/srv/kanjikyoushi/data/db.sqlite3` inside the
chroot (`/data/debian/srv/kanjikyoushi/data/db.sqlite3` from Android). Deploys never replace it, they only
run `migrate`. The first deploy copies the Mac's `backend/server/db.sqlite3` there, and from then on the
phone's copy is the real one. Back it up (see below) before anything risky.

## How it fits together

Magisk runs two boot scripts from `/data/adb/service.d/`. `service.sh` waits for the boot to finish, takes a
wake lock (without one the phone sleeps with the screen off and stops answering), adds a firewall rule (see
"Security"), then runs `chroot.sh start`. That starts `run.sh` in `/data/debian`, which supervises three
processes and restarts any that exit. `battery.sh` is independent of the app (see "Battery").

| Process | Runs as | Listens on |
|---|---|---|
| `tailscaled` (userspace networking) | root | none, reaches the tailnet |
| gunicorn serving Django, 2 workers | `kanji` | `127.0.0.1:8000` |
| Next.js standalone (`node frontend/server.js`) | `kanji` | `0.0.0.0:3000`, IPv4 only; the firewall admits loopback and home-network addresses |

The browser only talks to Next.js. The frontend is built with `NEXT_PUBLIC_HOST=/api`, and `next.config.mjs`
rewrites only `/api/home/*` and `/api/type/*` to Django on `127.0.0.1:8000`. Nothing else is forwarded, so
Django's admin and JWT token views are not reachable from outside (they 404). The site has two ways in and no
others: Tailscale (Serve hands it HTTPS requests from the tailnet, over loopback) and the home Wi-Fi, at
`http://<phone's IP>:3000` (plain HTTP, home only; currently `http://192.168.3.88:3000`). Never the internet.

Layout inside the chroot, under `/srv/kanjikyoushi/`:

| Path | What |
|---|---|
| `data/db.sqlite3` | the database, owned by `kanji` |
| `env` | `DJANGO_DEBUG=0`, `DJANGO_SECRET_KEY` (random, generated once), `DJANGO_ALLOWED_HOSTS`, `DJANGO_SQLITE_PATH`, `DJANGO_CACHE_DIR` (`data/cache`), `DJANGO_NUM_PROXIES=1` (Tailscale adds the client address; Next.js passes it on unchanged) |
| `backend/`, `frontend/` | current release; the previous one stays as `backend.old/`, `frontend.old/` |
| `venv/` | shared virtualenv, outside the code so swapping code never breaks it |
| `logs/` | `tailscaled.log`, `backend.log`, `frontend.log` (trimmed to 1 MB once over 10 MB) |
| `phone/` | copies of these scripts, used on the phone |

Corpus building (`build_corpus`, `load_dictionary`) stays on the Mac. The phone installs without fugashi,
cutlet, UniDic and psycopg. To change the corpus, run it on the Mac and ship the database.

## Security

- Nothing is public. Tailscale Serve (never Funnel) only answers devices in the tailnet and people the phone is
  shared with; to everyone else on the internet the site doesn't exist.
- Signup is invite-only (see "Invite codes").
- Rate limits (`settings.py`): anonymous 60/min, signed-in users 300/min, login 10/hour per account name,
  signup 5/hour per IP. The counters live in a file cache (`DJANGO_CACHE_DIR`) shared by the gunicorn workers.
- The Django admin and token views are not exposed, and gunicorn binds to loopback only.
- `service.sh` sets up the firewall:
  - Incoming: port 3000 only accepts loopback and private home-network addresses (192.168/16, 10/8,
    172.16/12); IPv6 to it is refused, since the phone may have a public IPv6 address.
  - Outgoing: uid 2900 (`kanji`) can answer connections made to it but can only start them over loopback, so
    a compromised app can't reach other devices on the Wi-Fi or the internet. Root processes such as
    `tailscaled` are not restricted.
- The app processes run as `kanji`, not root.

This does not cover a root-level compromise of the phone (a kernel exploit, say), which would bypass the
firewall, and the phone no longer gets HyperOS security updates because the updater was removed. Putting it on
a guest Wi-Fi network with client isolation is the step that limits that.

## Invite codes

Signup needs a one-time code. Make codes, or list unused ones, with the `invite` management command, run inside
the chroot as `kanji` with the env loaded. Both lines run from the Mac, with `C` as in "Logs, status, restarting":

```bash
adb shell "su -c '$C run bash -c \"set -a; . /srv/kanjikyoushi/env; set +a; setpriv --reuid=kanji --regid=kanji --init-groups /srv/kanjikyoushi/venv/bin/python /srv/kanjikyoushi/backend/server/manage.py invite --count 3 --note friends\"'"
adb shell "su -c '$C run bash -c \"set -a; . /srv/kanjikyoushi/env; set +a; setpriv --reuid=kanji --regid=kanji --init-groups /srv/kanjikyoushi/venv/bin/python /srv/kanjikyoushi/backend/server/manage.py invite --list\"'"
```

The first makes three codes noted "friends"; the second prints each unused code with its date and note.
Codes are 10 characters (no `0`, `O`, `1` or `I`), case-insensitive, and used up on signup.

## Battery

`battery.sh` keeps the always-plugged-in battery between 40% and 80% instead of at 100%. Every `SLEEP`=180 s it
uses `restrict_chg` and `restrict_cur` in `/sys/class/qcom-battery` to pause charging at `MAX`=80 and resume at
`MIN`=40 (defaults at the top of the file). USB stays online, so Android sees no unplug. It starts at the next
boot (`setup.sh` installs it but doesn't start it) and logs each change:

```bash
adb shell "su -c 'tail -n 20 /data/adb/kanjikyoushi-battery.log'"       # "pause at 80%", "resume at 40%"
adb shell "su -c 'cat /sys/class/qcom-battery/restrict_chg /sys/class/power_supply/battery/capacity'"
```

`restrict_chg` is `1` while charging is paused and the battery carries the load. If the loop dies then, nothing
resumes charging and the battery drains, so if the phone is ever unexpectedly low, check the log and
`restrict_chg` (writing `0` to it turns charging back on).

## First-time setup

On the Mac you need `adb`, `uv`, `npm`, `rsync` and `sqlite3`. On the phone, grant root to the adb Shell in
Magisk > Superuser, and plug it in with USB debugging on. `setup.sh` checks for root and stops if it is missing.

```bash
cd backend/deploy/phone
./setup.sh      # downloads Debian trixie-slim arm64 (cached in ~/.cache/kanjikyoushi-phone), unpacks it into
                # /data/debian, runs bootstrap.sh, installs both boot scripts in /data/adb/service.d/
./deploy.sh     # builds, copies the database on the first run, installs, starts
```

`setup.sh` is safe to rerun. `bootstrap.sh` installs pinned, checksummed Node, uv and Tailscale, creates
the `kanji` user (uid 2900), and writes `env` if it doesn't exist (an old one just gets any missing
`DJANGO_CACHE_DIR` and `DJANGO_NUM_PROXIES` lines).
The frontend must be checked out at `../frontend/client` relative to `backend/`, as in the `kanjikyoushi/`
folder. After setup, reboot the phone once to confirm the app comes back by itself.

## Tailscale

Run these once, inside the chroot (see "Getting a shell"):

```bash
tailscale up --hostname=kanjikyoushi   # prints a login link; open it and approve the device
tailscale serve --bg 3000              # tailnet-only HTTPS; the first time, it links to where you enable Serve
```

The site is then `https://kanjikyoushi.<tailnet>.ts.net` (here `kanjikyoushi.tail96317d.ts.net`), for your own
devices running Tailscale. To let a friend in, share the machine with them from the admin console (Machines >
kanjikyoushi > Share); they install Tailscale, accept, and open the same URL. Don't use `tailscale funnel`: that
would make it public. The login and serve config live in `/var/lib/tailscale` and survive reboots.

Worth doing in the admin console: an access rule so the phone itself can't start connections to your other
tailnet devices (tag it and give the tag no outgoing access). Then a compromised phone stays a dead end.

## Deploying

```bash
cd backend/deploy/phone
./deploy.sh
```

It builds the frontend (standalone output), packs the backend and these scripts, pushes one tarball, and
runs `release.sh` in the chroot: `uv sync`, `migrate`, swap the code in, then `run.sh restart-app`
(Django and Next.js only; `tailscaled` keeps running). It ends by printing the HTTP status of
`/login` on the phone; `200` means it is up. If you changed `service.sh`, `battery.sh` or `bootstrap.sh`, rerun `setup.sh`.

If a deploy breaks the app, put the previous code back from a shell inside Debian (see "Getting a shell").
Migrations are not undone:

```bash
cd /srv/kanjikyoushi && rm -rf backend frontend && mv backend.old backend && mv frontend.old frontend && phone/run.sh restart-app
```

## Logs, status, restarting

Every command below runs `chroot.sh` as root on the phone. Quote it exactly like this: adb joins its
arguments into one string, so `adb shell su -c "..."` without the outer quotes hands `su` only the first word.

```bash
C='sh /data/debian/srv/kanjikyoushi/phone/chroot.sh'
adb shell "su -c '$C run tail -n 50 /srv/kanjikyoushi/logs/backend.log'"   # or frontend.log, tailscaled.log
adb shell "su -c '$C run curl -s -o /dev/null -w %{http_code} http://127.0.0.1:3000/login'"
adb shell "su -c '$C run ps -eo user,pid,rss,args'"     # tailscaled, gunicorn (3), next-server
adb shell "su -c '$C run /srv/kanjikyoushi/phone/run.sh restart-app'"   # Django + Next.js
adb shell "su -c '$C stop'" && adb shell "su -c '$C start'"             # everything, including tailscaled
```

`restart-app` works because the supervisor restarts whatever it killed, two seconds later. Each restart
is logged in the process's log file as "exited with N, restarting in 2s".

## Getting a shell

```bash
adb shell -t "su -c 'sh /data/debian/srv/kanjikyoushi/phone/chroot.sh shell'"   # root shell inside Debian
```

To run Django commands, load the env file and use the venv as `kanji`:

```bash
set -a; . /srv/kanjikyoushi/env; set +a
setpriv --reuid=kanji --regid=kanji --init-groups /srv/kanjikyoushi/venv/bin/python \
    /srv/kanjikyoushi/backend/server/manage.py shell
```

## Backing up the database

```bash
./backup.sh            # saves ~/backups/kanjikyoushi/kanjikyoushi-DATE-TIME.sqlite3 (about 30 s for 1 GB)
```

It uses SQLite's online backup inside the chroot, so it's safe while the app is running. To restore, stop
the app, copy the file over `data/db.sqlite3`, `chown 2900:2900` and `chmod 600` it, and start again.

## Undoing and removing

Stop the app and keep it from starting at boot, after taking a backup:

```bash
adb shell "su -c '$C stop'"
adb shell "su -c 'rm /data/adb/service.d/kanjikyoushi.sh /data/adb/service.d/kanjikyoushi-battery.sh'"
```

Reboot to release the wake lock and the chroot mounts. With the battery script gone, charging limits reset at
that reboot. To delete everything, reboot first, then
`adb shell "su -c 'rm -rf /data/debian'"`. Do not run that while the chroot is mounted: `/data/debian/dev` is a
bind mount of Android's `/dev`. Remove the phone from the Tailscale admin console too, or run
`tailscale serve reset` inside the chroot to stop serving the site while keeping the device.
