# media-stack

Self-hosted media automation stack. This repo holds the **text config** —
`docker-compose.yml`, dnsmasq, qbit_manage, and the host-level scripts under
`host/`. Everything else (app databases, metadata, secrets) lives in the config
tarball described in [Backups](#backups).

A full rebuild needs **three** things:

| Piece | Where it lives |
|---|---|
| Compose, text config, host scripts | this repo |
| App databases + `.env` | `media-config-YYYY-MM-DD.tar.gz` |
| Media files | the `/data` disk |

Repo alone gives you a working but empty stack.

---

## Invariants

Rules that must hold. Each one, when broken, fails **quietly** — the stack keeps
running and looks healthy. Details are in the linked sections; this list exists
so you can check them without reading the whole document.

1. **`/data` is a single filesystem.** Never mount anything underneath it.
   Hardlinks between `torrents/` and `media/` only work within one filesystem,
   and when they stop working the *arrs silently fall back to copying — double
   the disk, no error. See [Storage layout](#storage-layout).

2. **`/data/.mounted` must exist on the real filesystem.** It is a sentinel, not
   data. It is **never created by automation** — creating it automatically would
   defeat its purpose, since a missing mount would simply get a fresh sentinel
   written into the empty directory. If you rebuild or reformat `/data`,
   `touch /data/.mounted` by hand. See
   [Monitoring and notifications](#monitoring-and-notifications).

3. **Every container that touches files needs `/data` mounted explicitly.**
   Sharing gluetun's network namespace shares no filesystem. See
   [qbit_manage](#qbit_manage).

4. **The `/data` mount point is identical in every container.** qbit_manage
   compares paths qBittorrent reports against paths it resolves itself; a
   mismatch makes every torrent look orphaned.

5. **The download side stays inside gluetun's namespace.** No split-tunnel, no
   exceptions. See [Architecture](#architecture).

6. **`root_dir` is `/data/torrents`, never `/data`.** Pointed at `/data`, every
   library file is flagged orphaned.

7. **No qBittorrent category ever points at `/data/media/`.** Torrents would
   download straight into the Jellyfin library. See
   [qBittorrent](#qbittorrent).

8. **A tracker missing from qbit_manage's `tracker:` block is treated as public
   and deleted on completion.** Add private trackers there *before* adding
   torrents from them.

9. **Jellyfin's media mount stays `:ro`.** Deletion happens in Sonarr/Radarr so
   their databases stay in sync.

10. **Hyper-V automatic checkpoints stay disabled.** Orphaned differencing disks
    filled `C:` once already. See [Gotchas](#gotchas).

---

## Architecture

```
                    ┌─────────────────────────────────────┐
                    │  gluetun (WireGuard → AirVPN NL)    │
                    │  network namespace shared by:       │
  LAN ──────────────┤   qbittorrent  qbit-manage          │
                    │   prowlarr  radarr  sonarr  lidarr  │
                    │   bazarr  flaresolverr  cleanuparr  │
                    │   chaptarr                          │
                    └─────────────────────────────────────┘

                    ┌─────────────────────────────────────┐
                    │  bridge network (media-stack_default)│
  LAN ──────────────┤   jellyfin  navidrome  jellyseerr   │
                    │   maintainerr  homepage  apprise    │
                    │   npm  dnsmasq                      │
                    └─────────────────────────────────────┘
```

Plex is gone — Jellyfin is the only media server.

**Why the namespace split.** Anything that talks to indexers or peers runs
inside gluetun's network namespace (`network_mode: "service:gluetun"`). They
have no network interface of their own — only gluetun's `tun0`. If the tunnel
drops, they have no route out. That's a structural killswitch, not a firewall
rule.

Consequences to remember:

- Those containers address each other over **`localhost`**, not container
  names. Radarr's download client is `localhost:8080`. Prowlarr's app URLs are
  `http://localhost:7878` etc.
- Their ports are published on the **gluetun** service, not on themselves.
  Adding a port for one of them means editing gluetun's `ports:` list.
- Never recreate gluetun alone. Its container ID is what the others attach to,
  so a solo recreate orphans them. Always
  `docker compose up -d --force-recreate`.
- **Every container in the namespace that touches files needs `/data` mounted
  explicitly.** Sharing a network namespace shares no filesystem. See the
  qbit_manage section for what happens when this is forgotten.
- They reach bridge-network containers by name (`http://apprise:8000` works from
  inside the namespace) because gluetun is itself attached to
  `media-stack_default`. Its firewall allows the local Docker subnet via
  `FIREWALL_OUTBOUND_SUBNETS`.

**Why one `/data` mount.** Hardlinks only work within a single filesystem. Every
container mounts `/data` whole — never `/data/media` and `/data/torrents`
separately — so Sonarr/Radarr can hardlink from torrents into the library
instead of copying. That's what lets qbit_manage delete finished public
torrents without touching the library.

**The `init-data` service** runs before anything else and acts as a **startup
gate**. It does two jobs:

1. **Asserts `/data/.mounted` exists.** If it doesn't, it prints a fatal message
   and exits non-zero. Because gluetun declares
   `depends_on: init-data: service_completed_successfully`, and every
   download-side container depends on gluetun, a failed assertion takes the
   whole stack down rather than letting it run against the wrong filesystem.
   This is deliberate: a stack that refuses to boot is a loud failure, and the
   alternative — running for 47 hours against an empty directory — has already
   happened once.

2. **Creates the `/data` tree and fixes ownership** to `PUID:PGID`. Necessary
   because Docker creates missing bind-mount sources as **root:root**, so on a
   fresh disk whichever container started first would silently create
   `/data/media` unwritable by the *arr apps.

The order matters — the assertion runs *before* the `mkdir -p`. Without it, an
unmounted `/data` would get the entire directory tree built on the OS root
filesystem, and the stack would run against that quite happily.

It's idempotent and does nothing when the structure is already correct.

---

## Repo layout

```
/opt/media-stack               ← this repo
├── docker-compose.yml
├── .env                       ← NOT in git; in the tarball
├── .env.example
├── host/                      ← host-level config, see below
└── config/
    ├── radarr/ sonarr/ lidarr/ prowlarr/ bazarr/    ← tarball only
    ├── jellyfin/ navidrome/ jellyseerr/             ← tarball only
    ├── chaptarr/ cleanuparr/ maintainerr/           ← tarball only
    ├── homepage/                                    ← tarball only
    ├── qbittorrent/ gluetun/ npm/                   ← tarball only
    ├── apprise/               ← tarball only (holds Discord webhook secrets)
    ├── dnsmasq/dnsmasq.conf   ← in git
    └── qbit-manage/config.yml ← in git
```

`config/apprise/` holds the three Discord webhook URLs in plaintext. Treat it
like `.env` — never commit it, and remember it's in the backup tarball.

### `host/`

Things that live outside `/opt/media-stack` on a running machine, kept here so
they survive an OS disk failure — including the backup machinery itself, which
would otherwise be lost exactly when it's needed.

| File | Deploys to | Notes |
|---|---|---|
| `install.sh` | — | run with sudo on a fresh host; deploys the rest |
| `backup-media-config.sh` | `/usr/local/bin/` | weekly config tarball |
| `media-backup.service` | `/etc/systemd/system/` | oneshot unit |
| `media-backup.timer` | `/etc/systemd/system/` | Fri 22:00, `Persistent=true` |
| `restore-config.sh` | — | run manually to restore a tarball |
| `netplan.yaml` | `/etc/netplan/00-installer-config.yaml` | **review first** — interface name and IP are machine-specific |
| `fstab.line` | appended to `/etc/fstab` | **review first** — UUID is disk-specific |
| `pull-media-config.ps1` | the Windows machine | scheduled task, not used on the VM |
| `indexers.txt` | — | which Prowlarr indexers were configured, and what to avoid |

`install.sh` deliberately does **not** apply netplan automatically. A wrong
netplan locks you out of SSH, and the UUID in `fstab.line` belongs to the old
disk.

---

## Storage layout

```
/data                          ← single ext4 mount (its own disk)
├── .mounted                   ← sentinel; created by hand, never by automation
├── torrents/
│   ├── movies/  tv/  anime/  music/  books/
│   │                          └── ebooks/  audiobooks/
│   └── orphaned_data/         ← qbit_manage quarantine, auto-created
└── media/
    ├── movies/  tv/  anime/  music/
    └── books/  └── ebooks/  audiobooks/
```

Created automatically by `init-data`, except `orphaned_data/` which qbit_manage
makes on first `rem_orphaned` run, and `.mounted` which you create manually.

`.mounted` sits at `/data`, deliberately **outside** `/data/torrents` — anything
inside `root_dir` would be picked up by qbit_manage's orphan scan.

Current disk is a 250 GiB VHDX, raw ext4 on the whole device with no partition
table — so the fstab UUID is the filesystem UUID directly. A move to an
external disk with LVM on top is planned but not done.

### Verifying hardlinks

The healthchecks confirm `/data` is mounted and writable, but **cannot** confirm
that `torrents/` and `media/` are the same filesystem — two correctly-mounted
but separate filesystems pass every check. Testing that means actually
attempting a link, which is a write, so it's a one-off check rather than a
continuous one:

```bash
touch /data/torrents/.hltest
ln /data/torrents/.hltest /data/media/.hltest && echo "HARDLINKS OK" || echo "BROKEN - separate filesystems"
rm -f /data/torrents/.hltest /data/media/.hltest
```

Run it after any storage change — a disk migration is the only thing that can
break it.

There's also a passive canary: qbit_manage's `tag_nohardlinks` reports
`checked N torrents, 0 with no hardlinks` on every run. If imports started
copying instead of linking, that second number would climb.

---

## Rebuild from scratch

### 1. Host

Ubuntu Server 22.04 LTS. Under Hyper-V: Generation 2, **Secure Boot disabled**,
**External virtual switch** so the VM gets a real LAN IP rather than sitting
behind host NAT.

Two disks: OS, and a separate one for `/data`.

Set a **static MAC** in the hypervisor (Hyper-V: Settings → Network Adapter →
Advanced Features) before anything else, so a router DHCP reservation can't be
orphaned later.

### 2. The `/data` disk

```bash
sudo mkfs.ext4 -L media /dev/sdb
sudo blkid /dev/sdb          # note the UUID
sudo mkdir /data
```

Add to `/etc/fstab`, substituting the new UUID (`host/fstab.line` has the old
one as a template). `nofail` matters — the VM still boots if the disk is
missing:

```
UUID=<uuid>  /data  ext4  defaults,nofail  0  2
```

```bash
sudo mount -a
sudo touch /data/.mounted        # sentinel — the stack will not start without it
```

The directory tree and ownership are handled by `init-data` at first
`docker compose up`.

### 3. Docker

From Docker's official repo — not snap, not `apt install docker.io`.

```bash
sudo apt install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo $VERSION_CODENAME) stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list
sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo usermod -aG docker $USER   # log out and back in
```

### 4. Clone and restore

```bash
sudo mkdir -p /opt/media-stack
sudo chown $USER:$USER /opt/media-stack
git clone git@github.com:USER/media-stack.git /opt/media-stack
cd /opt/media-stack

./host/restore-config.sh ~/media-config-YYYY-MM-DD.tar.gz
```

`restore-config.sh` moves any existing `config/` aside, extracts the archive,
fixes ownership, and brings the stack up.

If you have no tarball: copy `.env.example` to `.env`, fill it in,
`docker compose up -d`, and configure each app by hand — see
[First-time configuration](#first-time-configuration). Note that without the
tarball, `config/apprise/` is empty and the notification keys must be re-added —
see [Monitoring and notifications](#monitoring-and-notifications).

### 5. Static IP

Review `host/netplan.yaml`, correct the interface name (`ip -br a`) and gateway
(`ip route | grep default`), then:

```bash
sudo cp host/netplan.yaml /etc/netplan/00-installer-config.yaml
sudo chmod 600 /etc/netplan/00-installer-config.yaml
sudo netplan try
```

`netplan try` reverts after 120s if you lose the connection — safer than
`apply` over SSH.

IPv6 is disabled deliberately: on a Hyper-V wireless bridge it half-works, so
apps try IPv6 first and wait for a timeout before falling back.

Also add a DHCP reservation on the router for the VM's MAC. Belt and braces —
netplan asserts the address, the reservation stops the router offering it to
anything else.

Do not point the VM's resolver at its own dnsmasq container; it starts after
networking, so boot-time DNS would fail. The VM resolves via upstream
(`1.1.1.1`, `9.9.9.9`) and therefore **cannot itself resolve `*.home.arpa`** —
only clients with the split-DNS rule can. Fine in practice, but worth knowing
before writing a script on the VM that expects `jellyfin.home.arpa` to work.

### 6. Host services

```bash
sudo ./host/install.sh
```

Installs the backup script, enables the systemd timer, and appends the fstab
line if absent. Then copy `host/pull-media-config.ps1` to the Windows machine
and register the scheduled task — see [Backups](#backups).

### 7. Verify

```bash
docker compose ps                             # all up, gluetun healthy
docker logs init-data                         # "data structure verified"
docker exec gluetun wget -qO- https://ipinfo.io/ip   # VPN exit IP
curl -s ifconfig.io                           # your real IP — must differ
```

If those last two match, the tunnel isn't working. Stop and fix before
downloading anything.

Then verify peer connectivity, which is a **separate** thing from the tunnel
working — see [qBittorrent](#qbittorrent):

```bash
docker exec gluetun sh -c 'netstat -ln | grep 49113'   # expect 10.x.x.x, not 172.18.x.x
curl -s -b /tmp/qbc 'http://localhost:8080/api/v2/sync/maindata' \
  | jq '.server_state | {dht_nodes, connection_status}'
```

Expect `dht_nodes` in the hundreds and `connection_status: connected`.

Then verify monitoring — see
[Monitoring and notifications](#monitoring-and-notifications) for what each of
these proves:

```bash
docker ps --format '{{.Names}}\t{{.Status}}'                    # 8 containers (healthy)
curl -sS -X POST -F "body=rebuild test" http://localhost:8000/notify/p1
docker exec qbit-manage sh -c 'test -f /data/.NOPE && test -w /data/torrents; echo $?'   # must be 1
```

---

## `.env`

Never committed. Values live in the tarball.

```
PUID=1000
PGID=1000
TZ=Europe/Paris
DATA=/data
CONFIG=/opt/media-stack/config

WIREGUARD_PRIVATE_KEY=
WIREGUARD_ADDRESSES=
WIREGUARD_PRESHARED_KEY=
SERVER_COUNTRIES=Netherlands
SERVER_NAMES=
VPN_PORT=

QBIT_USER=
QBIT_PASS=
```

`SERVER_COUNTRIES` deliberately isn't France. `VPN_PORT` is the AirVPN
forwarded port for peer connectivity — it has nothing to do with remote access
to the stack.

**`SERVER_NAMES` is currently set in `.env` but not passed to the gluetun
container.** With only `SERVER_COUNTRIES` in the environment block, gluetun
picks any AirVPN server in that country and may pick a different one on each
reconnect. AirVPN port forwards are account-wide rather than server-specific, so
this doesn't break connectivity — but if you want a stable exit IP, add
`- SERVER_NAMES=${SERVER_NAMES}` to gluetun's environment. Otherwise drop the
variable from `.env` so it stops looking load-bearing.

---

## Image pinning

All images are pinned by digest (`repo@sha256:...`) with a trailing comment
recording the version and pin date. **gluetun is deliberately left at
`:latest`** — its VPN provider configs shift server-side and a stale image is
more likely to break the tunnel than a fresh one. **apprise is also on
`:latest`** pending a pin.

Consequence: `docker compose pull` is a no-op. Updating is deliberate —
temporarily replace a digest with a tag, `pull`, then re-pin. Regenerate all
pins from what's currently running:

```bash
docker ps --format '{{.Names}}' | while read n; do
  img=$(docker inspect "$n" --format '{{.Config.Image}}')
  docker image inspect "$img" --format '{{index .RepoDigests 0}}'
done
```

The version comments come from `org.opencontainers.image.version`, which is
unreliable for some images — chaptarr and cleanuparr report their Ubuntu base
(`24.04`), maintainerr reports a branch name, and npm/jellyseerr/dnsmasq report
nothing. Those carry `?` rather than a false version.

---

## Ports

| Service | Port | Published on |
|---|---|---|
| qBittorrent | 8080 | gluetun |
| Prowlarr | 9696 | gluetun |
| Radarr | 7878 | gluetun |
| Sonarr | 8989 | gluetun |
| Lidarr | 8686 | gluetun |
| Bazarr | 6767 | gluetun |
| Chaptarr | 8789 | gluetun |
| Cleanuparr | 11011 | gluetun |
| FlareSolverr | 8191 | gluetun (internal, not published) |
| BitTorrent peer port | `${VPN_PORT}` | gluetun (TCP+UDP) |
| Jellyfin | 8096 | itself |
| Jellyseerr | 5055 | itself |
| Navidrome | 4533 | itself |
| Maintainerr | 6246 | itself |
| Homepage | 3000 | itself |
| Apprise | 8000 | itself |
| NPM | 80 / 81 / 443 | itself |
| dnsmasq | 53 | itself |

---

## First-time configuration

Only needed if rebuilding without a config tarball. `host/indexers.txt` lists
which indexers were in use.

### qBittorrent

Default login `admin` / temp password in `docker logs qbittorrent`.

Four things that bite:

**1. Connection reset in the browser** → set
`WebUI\HostHeaderValidation=false` in
`config/qbittorrent/qBittorrent/qBittorrent.conf`. **Stop the container
first**; it rewrites the file on exit.

**2. Network Interface must be set to `tun0`** (Options → Advanced), not left
empty. Empty means "any", but qBittorrent enumerates interfaces at bind time
and gluetun's `tun0` doesn't exist yet — so it binds `eth0` + loopback only.
Outbound works, so nothing looks broken. But AirVPN's forwarded port arrives on
the tun0 address with nothing listening, the kernel returns RST, and you get
`connection_status: firewalled`, `dht_nodes: 0`, and every magnet stuck in
`metaDL` indefinitely — which looks like dead releases rather than a config
fault. Stored as `Session\Interface=tun0` and `Session\InterfaceName=tun0`.

This is also a killswitch improvement: bound to `tun0`, qBittorrent physically
cannot send traffic if the tunnel drops, rather than relying on gluetun's
firewall alone.

Verify after any rebuild or unclean shutdown:

```bash
docker exec gluetun sh -c 'netstat -ln | grep 49113'   # expect 10.x.x.x, not 172.18.x.x
```

**3. Automatic Torrent Management must be enabled.** Options → Downloads →
"Use Automatic Torrent Management" ON, "When Torrent Category changed" →
Relocate, "When Default Save Path changed" → Relocate. Without ATM, **category
save paths are ignored entirely** — a category is just a label and everything
lands in the Default Save Path. Symptom is every torrent piling up at
`/data/torrents/` root regardless of category, which makes orphan detection
hard to read and gets worse as categories multiply.

ATM applies to new torrents only. Existing ones stay in Manual mode until
switched individually (right-click → Automatic Torrent Management), at which
point qBittorrent relocates them. Harmless either way — moves within `/data`
are renames and don't affect hardlinks.

**4. Default save path** must be `/data/torrents/`.

Categories, each with its save path:

| Category | Save path |
|---|---|
| `radarr` | `/data/torrents/movies` |
| `tv-sonarr` | `/data/torrents/tv` |
| `anime-sonarr` | `/data/torrents/anime` |
| `lidarr` | `/data/torrents/music` |
| `books` | `/data/torrents/books` |

Create these before pointing the *arr apps at them — a missing category makes
qBittorrent reject the add with **409 Conflict**, and the *arr app just reports
a failed grab.

**Never point a category at `/data/media/`.** Torrents would download directly
into the Jellyfin library: no hardlink separation, release-group junk and
partial files in the library, and deleting the torrent with "also delete files"
takes the library copy with it. The `books` category was misconfigured this way
and it would have corrupted the Readarr migration.

Verify all five:

```bash
curl -s -b /tmp/qbc 'http://localhost:8080/api/v2/torrents/categories' \
  | jq -r 'to_entries[] | [.key, .value.savePath] | @tsv'
```

### Prowlarr

Add indexers, then Settings → Apps → add each *arr:

- Prowlarr Server: `http://localhost:9696`
- App Server: `http://localhost:7878` / `:8989` / `:8686` / `:8789`
- API key from each app's Settings → General

Then **Sync App Indexers**. FlareSolverr goes in at `http://localhost:8191`
for Cloudflare-protected indexers.

Adding an indexer later does **not** retroactively search past failures. Go to
Sonarr → Wanted → Missing → Search to retry with the new indexer.

### Radarr / Sonarr / Lidarr

- Media Management → **Use Hardlinks instead of Copy** ON
- Root folders: `/data/media/movies`, `/data/media/tv`, `/data/media/anime`,
  `/data/media/music`
- Download client: qBittorrent at `localhost:8080`, matching category
- Leave the download client's optional **Directory** field EMPTY. A value there
  is passed per-torrent and overrides the category save path.
- Sonarr min free space: 10000 MB
- Jellyfin connection: Settings → Connect → Emby/Jellyfin →
  `192.168.1.16:8096`, API key from Jellyfin, **Update Library** ON
- Discord notifications: see
  [Monitoring and notifications](#monitoring-and-notifications)

**Anime: set Series Type to `Anime`.** Sonarr defaults every series to
`Standard`, and anime releases use absolute numbering (`EP1170`) that Standard
can't parse. Symptom: Prowlarr's own search finds releases, Sonarr's finds
nothing. Fix on the series → Edit → Series Type → Anime.

**Lidarr builds the album folder inside the track format** — there is no
separate Album Folder Format field like Sonarr and Radarr have. Working values:

- Standard Track Format:
  `{Album Title} ({Release Year})/{Artist Name} - {Album Title} - {track:00} - {Track Title}`
- Multi Disc Track Format:
  `{Album Title} ({Release Year})/{Medium Format} {medium:00}/{Artist Name} - {Album Title} - {track:00} - {Track Title}`
- Artist Folder Format: `{Artist Name}`

**Rename Tracks must be ON** (Advanced toggle) or none of that applies and
imports keep their original torrent filenames — producing a flat artist folder
where two albums with a `01 Intro.flac` collide. To fix an existing flat
library: Artists → Mass Editor → select all → Rename Files, then restart
Navidrome so it rescans the new paths.

Lidarr's `Standard` metadata profile is studio albums only — no singles, EPs,
live, or compilations. Widen it per-artist if needed. Music releases are often
tagged `Unknown` quality, which the `Standard` quality profile rejects; use
`Any` for music.

### Books (Chaptarr → Readarr)

Chaptarr is the currently deployed books app, on port 8789. Its add-book path
is broken in the dev build, which is why the migration to **Readarr +
rreading-glasses** is planned — plain Readarr needs the third-party metadata
service because the official one was retired.

Chaptarr sends only a category name (`books` for both ebooks and audiobooks)
and lets qBittorrent resolve the path — no directory override. Its
`musicCategory` field is a vestige of the Readarr fork lineage and points at a
category that doesn't exist; harmless, but clear it if it bothers you.

Root folders: `/data/media/books/ebooks` and `/data/media/books/audiobooks`.
Both share one torrent save path and separate at import time by root folder.
Readarr works the same way, so this carries over.

**Nothing reads `/data/media/books` yet.** The books side is still being built —
acquisition and organisation are in place, the reading/listening end isn't. Not
an omission; just unfinished. Books are deliberately not a Jellyfin library.

**Before adding `books` to qbit_manage's `nohardlinks` list**, verify a book
import actually produces 2 links. If the books app copies instead of
hardlinking, every book torrent would be tagged `noHL` incorrectly.

### Jellyfin

- Libraries point at `/data/media/{movies,tv,anime,music}`, mounted `:ro`.
  Books are deliberately not a Jellyfin library — see [Books](#books-chaptarr--readarr).
- Scheduled library scan: **1 hour**. Real-time monitoring and the Sonarr
  notification both rely on inotify, which hardlinks don't reliably fire — so
  the scheduled scan is what actually catches new content.
- Audio: preferred language English, **"Play default track regardless of
  language" UNCHECKED** (otherwise dual-audio files default to the wrong track)

Because the media mount is read-only, **deleting from Jellyfin fails by
design**. Delete in Sonarr/Radarr instead, so their databases stay in sync and
they don't immediately re-download what you removed.

Playback on the Samsung TV goes through an Android TV / Google TV device.
Sideloading Jellyfin onto Tizen directly is a dead end — certificate failure.

### Bazarr

Languages French + English. Connect to Sonarr/Radarr on `localhost`.

TRaSH scoring: Sonarr minimum score 90, Radarr 80; sync thresholds Series 96,
Movies 86.

Bazarr is **not** an *arr — different codebase, different API, and Apprise is a
native notification provider rather than a Connect type. Its notifications are
configured by hand and are not covered by the provisioning script.

### Navidrome

`ND_MUSICFOLDER=/data/media/music`, `ND_SCANSCHEDULE=1h`. Its `/data` volume is
config, and the music mount is `:ro`.

No push notification from Lidarr exists, so new music appears on the hourly
scan. After a Lidarr mass rename, restart Navidrome — its database holds the
old paths and the library looks broken until it rescans.

iPhone: **Amperfy** or **play:Sub**, Subsonic server `http://192.168.1.16:4533`.
Offline download actually works here, unlike Jellyfin video on iOS.

### Jellyseerr

Setup wizard → Jellyfin:

- URL `192.168.1.16`, port `8096`, SSL off
- **URL Base must be EMPTY.** A bare `/` fails validation; `/ ` with a trailing
  space produces a 404 on the auth endpoint that looks like a version
  incompatibility but isn't.

Services → Radarr and Sonarr at `192.168.1.16:7878` / `:8989`.

- **Tag Requests OFF.** It tries to create a Radarr tag, gets a **400**, and the
  whole request fails. Requester identity is tracked in Jellyseerr's own
  Requests page regardless.
- Sonarr entry has separate anime fields — set **Anime Series Type: Anime** and
  **Anime Root Folder: `/data/media/anime`**, so anime requests arrive with the
  right series type instead of defaulting to Standard.

Season numbering for anime often disagrees between TMDB (Jellyseerr) and TVDB
(Sonarr) — a request covering "season 1" in Jellyseerr may leave later Sonarr
seasons unmonitored. Check monitoring in Sonarr after requesting anime.

### Cleanuparr

Runs inside gluetun's namespace, reaching each *arr on `localhost`. Jobs:
QueueCleaner every 5 minutes, Seeker every 10.

Each *arr is registered individually with its own API key. It health-checks
them on startup and logs `Unhealthy` for anything not yet up — expected noise
during a cold start, and it recovers on its own.

Its main job is removing downloads that stall with no connections, blocklisting
the release in the *arr, and triggering a re-search so a different release gets
grabbed. That only works if peer connectivity is actually functional — see the
qBittorrent `tun0` note, since a broken bind makes *everything* look stalled
and Cleanuparr will dutifully blocklist perfectly good releases.

### Maintainerr

Rule-based library retention. Collections sync to Jellyfin as a "leaving soon"
shelf, and items are deleted once they have sat there for **Take action after
days**.

Set the **Radarr/Sonarr action** to delete *and* unmonitor, and leave the
Jellyfin action as none — the media mount is `:ro`, so a media-server delete
fails, and deleting without unmonitoring means the *arr re-downloads within the
hour.

**`BEFORE` and `AFTER` are not symmetric with `custom_days`.** A value of 30
resolves to:

- `BEFORE 30` → 30 days in the **past** (item is older than that) — what you want
- `AFTER 30` → 30 days in the **future** — never true for existing media

So `AFTER` cannot express "within the last N days", and any condition using it
silently matches nothing. Two-sided date windows are therefore impossible;
overlapping collections between an aged-out rule and a low-space rule are the
accepted trade-off (the shorter grace period fires first).

Use **Test Media** on a single title to see the resolved comparison dates
before trusting any rule — it prints `firstValue` and `secondValue`, which is
how the above was found.

Field-name traps:

- `sw_` means show-scope, **not** Streamystats. Movies use
  `Jellyfin.viewCount`; seasons and shows use `Jellyfin.sw_amountOfViews`.
- `Sonarr.addDate` is show-scope only — use `Jellyfin.addDate` in season rules.
- `unaired_episodes` is show/season scope; `unaired_episodes_season` is
  episode scope.
- Add `Sonarr - Is (part of) latest aired/airing season` = false to protect the
  season you are currently watching.

Rules are exported as YAML from the UI and kept in `maintainerr/` in this repo.
`POST /api/rules/yaml/decode` is what the import uses, if you ever script it.

### Homepage

Dashboard on :3000, bridge network. Config is hand-written YAML under
`config/homepage/` — worth considering for the git allowlist alongside dnsmasq
and qbit_manage, since it's text you edit rather than state an app manages.

### qbit_manage

Seeds private trackers indefinitely; deletes public torrents once downloaded.
The library survives via hardlink.

```yaml
environment:
  - QBT_RUN=false        # true means run-once-and-exit
  - QBT_SCHEDULE=30      # minutes
volumes:
  - ${CONFIG}/qbit-manage:/config
  - ${DATA}:/data        # REQUIRED — see below
restart: on-failure:2
```

**The `/data` mount is not optional.** Sharing gluetun's network namespace
shares no filesystem. With only `/config` mounted, `tag_update` and
`share_limits` work fine — they're pure qBittorrent API calls — while every
filesystem-dependent command silently does nothing. This went undetected for
47+ hours. The first symptom was `rem_orphaned` crashing with
`PermissionError: '/data'` when it tried to create the orphaned directory,
because `os.makedirs` walked up to a `/data` that didn't exist in the container.

This is now guarded twice: `init-data` refuses to complete without
`/data/.mounted`, and the container carries a healthcheck asserting the mount is
present and writable. See
[Monitoring and notifications](#monitoring-and-notifications).

The mount point must be **identical** to qBittorrent's. qbit_manage compares
the paths qBittorrent reports against paths it resolves itself; a mismatch makes
every torrent look orphaned. That's what `remote_dir` exists to patch, and you
don't want to need it.

**`QBT_RUN=true` plus `restart: unless-stopped` is an infinite loop** — the
container exits, Docker restarts it, and cleanup runs continuously. It will
delete downloads before Lidarr has imported them. The log line to check is
`Run Mode: Script will exit after completion` versus
`30 minutes until the next run`.

#### `config/qbit-manage/config.yml`

Commands currently enabled: `tag_update`, `share_limits`, `rem_orphaned`,
`tag_nohardlinks`. `dry_run: false`.

**`dry_run` is global, not per-command.** Leaving it `true` as a safety measure
disables tagging and share limits too. Control risk with the individual command
flags instead.

```yaml
directory:
  root_dir: /data/torrents          # required even for tagging
  orphaned_dir: /data/torrents/orphaned_data

orphaned:
  empty_after_x_days: 14
  max_orphaned_files_to_delete: 50
  min_file_age_minutes: 60
  exclude_patterns:

nohardlinks:                        # top-level key, NOT under commands
  radarr:
  tv-sonarr:
  anime-sonarr:
  lidarr:
```

Notes:

- **`root_dir` must be `/data/torrents`, never `/data`.** Pointed at `/data`,
  every file in the library gets flagged as orphaned — none of them are torrent
  contents in qBittorrent's view.
- **`orphaned_dir` inside `root_dir`** keeps the move a same-filesystem rename
  instead of a copy. qbit_manage auto-appends the quarantine folder to
  `exclude_patterns` so it can't rescan its own output.
- **`max_orphaned_files_to_delete` aborts the whole sweep** if exceeded — it
  does not move the first N. The abort logs the count and full file list and
  moves nothing. That guard is what catches a misconfigured `root_dir`. Raise it
  temporarily for a genuine large cleanup, then put it back.
- **The abort now fires a P1 Discord alert** via the `webhooks:` block below.
  Before that was configured, it was invisible outside `docker logs` — nothing
  in Jellyfin, Homepage or qBittorrent surfaced it.
- **`min_file_age_minutes: 60`** stops a sweep catching files mid-import, when
  they exist on disk but aren't yet claimed by a torrent.
- **`empty_after_x_days: 14`** deletes from quarantine on that schedule — it
  runs independently of the move, so files already quarantined still age out
  even on a run that aborts.
- **`tag_nohardlinks` needs a top-level `nohardlinks:` list of categories.**
  Enabling the command without it produces
  `Config Error: nohardlinks must be a list of categories` and **exits the
  entire run** — so `rem_orphaned` and `share_limits` stop working too.
- `recyclebin.enabled: false` — enabling it tries to create `/data/.RecycleBin`
  and fails on permissions
- `min_seeding_time` is rejected alongside `max_ratio: -1`; drop it, since
  `cleanup: false` already protects private torrents
- credentials via `!ENV QBIT_USER` / `!ENV QBIT_PASS`, passed through from
  compose

**What `noHL` actually means.** A torrent with one link has no library
counterpart — usually because you deleted the film from the library and the
torrent kept seeding. It is *not* primarily a failed-import detector. It answers
"what am I still seeding that I no longer keep?", which is the drift that
accumulates when you curate and delete.

#### Share limits and tracker classification

```yaml
share_limits:
  public:
    priority: 1
    include_all_tags: [public]
    max_ratio: 0
    max_seeding_time: 0
    cleanup: true            # ← the only delete path in the whole config
  private:
    priority: 2
    include_all_tags: [private]
    max_ratio: -1            # -1 = unlimited
    max_seeding_time: -1
    cleanup: false
```

`max_ratio: 0` means public torrents are deleted the moment they are checked
after completing — effectively zero seeding on public trackers. Deliberate, but
worth knowing: nothing is ever given back on those. The library copy survives
via hardlink.

Classification comes from the `tracker:` block. **A torrent from a tracker not
listed there falls into `public` and gets deleted on completion.** So adding a
new private tracker means adding it to `tracker:` *first*, or you take a
hit-and-run on a tracker that counts them.

MyAnonaMouse is already listed ahead of joining. Joining also needs the
`mam_id` session token and the Dynamic Seedbox API call made **from inside
gluetun's namespace**, so the IP matches.

Being tagged `private` is a choice, not a fact about the tracker — open
trackers with no ratio requirement will seed forever for no benefit if listed.

---

## Monitoring and notifications

Built after a session that turned up two silent failures: qbit_manage running
47+ hours with no `/data` mount, and `rem_orphaned` detecting 831 orphans,
exceeding its threshold, aborting, and leaving no trace outside `docker logs`.
The design goal is narrow — **be told when automation aborts or breaks** — not
general observability.

### Shape

```
  qbit-manage ──┐
  radarr        │
  sonarr        ├──→  apprise (bridge, :8000)  ──→  Discord
  lidarr        │      keys: p1 / p2 / info          #p1-critical
  prowlarr    ──┘                                    #p2-action
  bazarr (manual)                                    #info
```

Everything points at a **label** (`http://apprise:8000/notify/p1`), never at a
Discord URL. The three webhook secrets live only in `config/apprise/`. Swapping
Discord for something else later is three `curl` commands and no service config
changes.

apprise runs on the **bridge network, not inside gluetun** — it is the component
that reports on everything else, so it must not share a failure domain with the
VPN.

### Channels and severity

| Channel | Key | What lands there |
|---|---|---|
| `#p1-critical` | `p1` | qbit_manage errors and aborts, *arr health issues |
| `#p2-action` | `p2` | manual import required, cleanup_dirs, tracker errors |
| `#info` | `info` | routine activity |

**Per-channel notification overrides are load-bearing.** Discord mutes at the
*server* level, so if `#info` gets noisy and you mute the server, P1 dies with
it. Set `#p1-critical` and `#p2-action` to All Messages and `#info` to Nothing.

**Discord suppresses mobile push while another client looks active** — default
10 minutes, under User Settings → Notifications → Push Notification Inactive
Timeout. A P1 arriving while you're at the laptop is a desktop badge, not a
phone buzz. Acceptable here because nothing in this stack is time-critical. The
escape hatch, if it ever matters, is adding a role mention to P1 payloads.

### apprise keys

Set once; they live in `config/apprise/` and are in the backup tarball. To
(re)create after a rebuild without a tarball:

```bash
curl -sS -X POST -F "urls=discord://<id>/<token>" http://localhost:8000/add/p1
curl -sS -X POST -F "urls=discord://<id>/<token>" http://localhost:8000/add/p2
curl -sS -X POST -F "urls=discord://<id>/<token>" http://localhost:8000/add/info
```

A Discord webhook URL ending `/api/webhooks/<id>/<token>` becomes
`discord://<id>/<token>`. Test:

```bash
curl -sS -X POST -F "title=Test" -F "body=p1 path works" http://localhost:8000/notify/p1
```

`Successfully saved configuration` on `add` only validates the URL *shape* — it
does not prove the endpoint exists. Only a `notify` that logs
`Sent Discord notification` and lands in the right channel proves the mapping.

### qbit_manage webhooks

**Per-function slots nest under `function:`.** Only `error`, `run_start` and
`run_end` are top-level. Wrong nesting is accepted silently — qbit_manage logs a
`Config Warning`, rewrites the key as null, and carries on with no
notifications. Check `docker logs qbit-manage | grep -i "config warning"` after
any edit.

```yaml
webhooks:
  error: http://apprise:8000/notify/p1
  run_start:
  run_end:
  function:
    rem_orphaned: http://apprise:8000/notify/p1
    rem_unregistered: http://apprise:8000/notify/p1
    cleanup_dirs: http://apprise:8000/notify/p2
    tag_tracker_error: http://apprise:8000/notify/p2
    cross_seed:
    recheck:
    cat_update:
    tag_update:
    tag_nohardlinks:
    share_limits:
```

Nulls are deliberate rather than omitted. `run_start`/`run_end` on a 30-minute
schedule is 96 messages a day. `tag_nohardlinks` would fire on every normal
download, since a torrent legitimately has one link between finishing and being
imported.

To test the orphan abort without waiting for a real one — it cannot self-test,
since zero orphans can't exceed any threshold:

```bash
mkdir -p /data/torrents/_orphantest
touch /data/torrents/_orphantest/a.bin /data/torrents/_orphantest/b.bin
```

Then set `max_orphaned_files_to_delete: 1`, `min_file_age_minutes: 0`,
`dry_run: true`, and restart. Two orphans against a limit of one reproduces the
original condition. Revert to `50` / `60` / `false` and remove the directory
afterwards.

### *arr Connect notifications

Configured over the REST API rather than by hand — see
`host/provision-arr-notifications.py`. It reads one hand-made Sonarr
notification as a template and clones it across Radarr, Lidarr and Prowlarr,
filtering trigger flags against each app's own schema so per-app differences
(Prowlarr has no `onManualInteractionRequired`, Lidarr's import triggers differ)
are handled by the apps rather than assumed.

To bootstrap: create one notification in Sonarr named `P1`, type Apprise, Server
URL `http://apprise:8000`, Configuration Key `p1`, trigger **On Health Issue**
only, **Include Health Warnings OFF**.

That last toggle matters. Health *warnings* behind a VPN include "indexer
unavailable" and "download client unavailable", which fire on every gluetun
reconnect and every resume-from-sleep. Errors only.

Bazarr is excluded — different API, configured by hand.

### Healthchecks

Seven containers that mount `/data` read-write carry:

```yaml
test: ["CMD-SHELL", "test -f /data/.mounted && test -w /data/<its own directory>"]
```

Two assertions doing different jobs:

- **`test -f /data/.mounted`** proves it's the *real* filesystem. A bind mount to
  an empty host directory passes `test -d /data/torrents` but fails this,
  because the sentinel only exists on the actual disk.
- **`test -w`** proves it's writable — catches ext4 remounting read-only after
  an I/O error, which becomes a realistic failure once `/data` is on external
  USB.

Each *arr tests its own library path rather than a shared one, so a partially
correct mount (TV present, music missing) is caught.

Jellyfin and Navidrome are deliberately excluded: they mount `:ro`, so `test -w`
fails by design, and a missing mount there produces an empty library — loudly
visible, not a silent failure.

Verify the check can actually fail, not just pass:

```bash
docker exec qbit-manage sh -c 'test -f /data/.NOPE && test -w /data/torrents; echo $?'   # must be 1
```

### What this does not cover

- **Hardlinks working between `torrents/` and `media/`.** Two correctly-mounted
  but separate filesystems pass every check. See
  [Verifying hardlinks](#verifying-hardlinks).
- **`unhealthy` reaching Discord.** Docker does nothing with the status — no
  restart, no alert. Homepage shows it; nothing pushes it. Closing that loop
  means a small container watching `docker events` for
  `health_status: unhealthy`. Note that autoheal is the *wrong* tool: restarting
  a container with a missing mount produces a container with a missing mount.
- **qbit_manage stopping entirely.** Nothing alerts on the *absence* of runs. A
  dead-man's switch (Healthchecks.io pinged from `run_end`, generous grace
  window for the laptop being off) would cover it. Not implemented.
- **apprise itself dying.** It has a healthcheck but nothing external watches it.
  Structurally, the notifier cannot report its own death.

---

## Local DNS and reverse proxy

Cosmetic, not required — everything works on `192.168.1.16:PORT`.

**dnsmasq** answers `*.home.arpa` with the VM's IP and forwards the rest
upstream. `.home.arpa` is the RFC 8375 reserved name for this; `.local` would
collide with mDNS.

**NPM** listens on :80 and routes by hostname to each published port. Tick
**Websockets Support** on every proxy host — Jellyfin and the *arr UIs need it.

| Hostname | Target | Service |
|---|---|---|
| `jellyfin.home.arpa` | 8096 | Jellyfin |
| `requests.home.arpa` | 5055 | Jellyseerr |
| `music.home.arpa` | 4533 | Navidrome |
| `home.home.arpa` | 3000 | Homepage |
| `sonarr.home.arpa` | 8989 | Sonarr |
| `radarr.home.arpa` | 7878 | Radarr |
| `lidarr.home.arpa` | 8686 | Lidarr |
| `prowlarr.home.arpa` | 9696 | Prowlarr |
| `bazarr.home.arpa` | 6767 | Bazarr |
| `qbit.home.arpa` | 8080 | qBittorrent |
| `cleanuparr.home.arpa` | 11011 | Cleanuparr |
| `maintainerr.home.arpa` | 6246 | Maintainerr |
| `nginx.home.arpa` | 81 | NPM itself |

All forward to `192.168.1.16` — the LAN IP, not container names, because the
namespaced apps have no addressable names of their own.

**Chaptarr (8789) has no proxy host** — reach it on `192.168.1.16:8789`. Add one
when Readarr replaces it. **Apprise (8000) has none either** and doesn't need
one — it's addressed container-to-container, never from a browser.

Read the live list without the UI (NPM ships no `sqlite3` binary, so use the
host's Python):

```bash
python3 - <<'EOF'
import sqlite3, json
db = '/opt/media-stack/config/npm/data/database.sqlite'
con = sqlite3.connect(f'file:{db}?mode=ro', uri=True)
for names, host, port, en in con.execute(
    "select domain_names, forward_host, forward_port, enabled from proxy_host where is_deleted=0"):
    for d in json.loads(names):
        print(f"{d:<32} -> {host}:{port}{'' if en else '   (disabled)'}")
EOF
```

**Client side**, split DNS so only `.home.arpa` goes to the VM. Otherwise the
VM becomes a single point of failure for all name resolution.

Windows:

```powershell
Add-DnsClientNrptRule -Namespace ".home.arpa" -NameServers "192.168.1.16"
```

Linux — `/etc/systemd/resolved.conf.d/homearpa.conf`:

```ini
[Resolve]
DNS=192.168.1.16
Domains=~home.arpa
```

The `~` prefix makes it routing-only. Android and iOS can't do split DNS, so
use IP:port in those apps — a one-time entry.

Don't route container-to-container traffic through the proxy. Same-namespace
apps on `localhost` are as direct as it gets; adding DNS and nginx to that path
only creates failure modes.

### DNS baseline (relevant to any future Tailscale work)

```
resolv.conf mode: uplink        # systemd-resolved is NOT stub-listening
:53 held by                     # docker-proxy only, fronting the dnsmasq container
eth0 DNS servers                # 1.1.1.1, 9.9.9.9
```

No conflict to untangle. Any split-DNS arrangement for `home.arpa` starts from a
clean state.

---

## Backups

**VM** — `backup-media-config.sh`, run by `media-backup.timer` every Friday
22:00 with `Persistent=true`, so a missed run fires at next boot. It stops the
stack (SQLite copied mid-write restores corrupt), tars `config/` and `.env`,
restarts, and keeps 1 archive.

**Windows** — a scheduled task each Saturday 10:00 runs
`pull-media-config.ps1`: pulls the archive, verifies byte size, deletes the
remote copy, keeps the last 2 locally. Registered with:

```powershell
$action  = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$env:USERPROFILE\Backups\pull-media-config.ps1`""
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Saturday -At 10am
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries
Register-ScheduledTask -TaskName "Pull media-stack config backup" -Action $action -Trigger $trigger -Settings $settings
```

It needs a passwordless SSH key at `$env:USERPROFILE\.ssh\id_ed25519_plex`.

The archive contains `.env` **and `config/apprise/`**, so it holds your
**WireGuard private key, qBittorrent password, and Discord webhook tokens in
plaintext**. Keep the backup folder on an encrypted disk.

Restore:

```bash
./host/restore-config.sh /path/to/media-config-YYYY-MM-DD.tar.gz
```

### Database / disk mismatch

The two halves can drift, and the consequences aren't symmetric:

**Databases newer than `/data`** (restoring a tarball onto an empty disk) — the
*arr apps believe they have a full library, find it missing, and **re-download
everything monitored**. That's usually what you want on a genuine rebuild, but
it's a lot of bandwidth at once and will hammer your indexers into rate limits.
If the media exists elsewhere, copy it into `/data/media` *before* starting
Sonarr/Radarr/Lidarr. Note that availability decays — old releases lose
seeders, so a restore returns your configuration perfectly and your library
approximately.

**`/data` newer than the databases** — Jellyfin and Navidrome recover on their
own, since they derive everything from the filesystem. The *arr apps won't:
they don't adopt files they don't know about, and may re-download content
already on disk. Fix with **Library Import** (Add New → Import Existing) before
letting them search.

---

## Gotchas

**The stack won't start if `/data/.mounted` is missing.** That's `init-data`
doing its job, not a bug. Symptom is gluetun never starting and the entire
download side down; the cause is in `docker logs init-data`, not in gluetun.
Check there first. Recreate with `touch /data/.mounted` once the disk is
genuinely mounted.

**Never recreate gluetun alone.** Use
`docker compose up -d --force-recreate` for everything.

**Adding a port for a namespaced app** means adding it to gluetun's `ports:`,
not the app's.

**Volume changes need `up -d`, not `restart`.** A restart reuses the existing
container with its existing mounts. Same for environment variables — env is set
at container creation. Adding a healthcheck also forces recreation, so do it
when nothing is mid-import.

**`Up` is not `healthy`, and `healthy` is not `working`.** Before the
healthchecks existed, Docker reported every container as `Up` regardless of
internal state — which is exactly why the missing `/data` mount survived 47
hours. The healthchecks narrow the gap; they don't close it. See
[what this does not cover](#what-this-does-not-cover).

**Silent failures are the recurring hazard here.** Three found in one session,
none of which surfaced anywhere: the missing `/data` mount in qbit_manage,
qBittorrent binding `eth0` instead of `tun0`, and qbit_manage aborting on an
orphan count above threshold. All three looked like healthy containers. Where a
feature appears to do nothing, check mounts and interface bindings before
assuming the feature is wrong.

**qBittorrent silently binds the wrong interface** if `Session\Interface` is
missing. Symptom is stalled magnets and zero DHT nodes, which reads as dead
releases. See [qBittorrent](#qbittorrent).

**Category save paths do nothing without ATM enabled.** See
[qBittorrent](#qbittorrent).

**`du -sh` on a directory of hardlinks overstates it wildly.** Each invocation
counts an inode once, so a quarantine folder full of hardlinked files can report
more than the entire filesystem holds. Use `df` to judge actual free space, and
`find -links 1` to find data that genuinely occupies space once.

**Deleting a hardlink frees nothing if another name remains.** Removing the
`torrents/` copy of an imported file is safe and reclaims zero bytes; the
library name still resolves to the same inode.

**Orphan sweeps delete content you may have wanted.** "Orphaned" means "no
torrent in the session", not "worthless". Music downloaded outside Lidarr and
never imported looks identical to genuine junk. Check the dry-run list against
what each *arr thinks it has before a large sweep.

**Hyper-V automatic checkpoints are on by default** on Windows client. They
create differencing disks that grow indefinitely and can fill the host drive,
at which point Hyper-V pauses the VM (`Paused-Critical`). Disable them:

```powershell
Set-VM -Name "VM Name" -AutomaticCheckpointsEnabled $false
```

If a chain already exists, merge it with `Merge-VHD` — a merge needs free space
roughly equal to the delta, so make room first. After merging outside Hyper-V's
awareness it may refuse disk changes with "a disk merging is pending"; detach
and re-attach the disks with `Remove-VMHardDiskDrive` / `Add-VMHardDiskDrive`
to clear the stale state. Detaching does not delete the files.

**`docker compose up -d` won't apply a new image** if the container is already
running and unchanged. Use `--force-recreate` after a `pull`. With digest pins,
`pull` is a no-op — see [Image pinning](#image-pinning).

**Sonarr won't cancel redundant grabs.** If a season pack and individual
episodes both download, clear the extras from Sonarr → Activity → Queue with
"remove from download client" ticked. It won't work it out on its own.

---

## Migrating to new hardware

The single-`/data`-mount and bind-mounted-config design means this is a copy,
not a rebuild:

1. Install Ubuntu + Docker on the new box (steps 1 and 3 above)
2. **Attach the media disk and mount it at `/data` via fstab** (step 2 above)
3. **Confirm `/data/.mounted` exists** — `ls -la /data/.mounted`. It travels with
   the disk, so it should already be there. If the disk is new or reformatted,
   `touch` it, or nothing will start.
4. `git clone` this repo to `/opt/media-stack`
5. `./host/restore-config.sh <tarball>` — extracts config and starts the stack
6. `sudo ./host/install.sh` — backup script and systemd timer
7. Review and apply `host/netplan.yaml`, then `sudo netplan try`
8. **Verify the qBittorrent `tun0` bind and DHT node count** — see step 7 of
   the rebuild. This setting lives only in the config tarball, not in compose.
9. **Verify hardlinks and notifications** — see
   [Verifying hardlinks](#verifying-hardlinks) and step 7 of the rebuild.

**Order matters at step 2.** `restore-config.sh` ends by starting the stack, and
`init-data` creates the `/data` tree on whatever `/data` currently is. The
sentinel assertion now catches this — an unmounted `/data` has no `.mounted`, so
`init-data` refuses rather than building the tree on the OS disk where it would
be hidden the moment the real disk mounts over it. Storage still comes before
containers; the guard is a backstop, not a substitute for ordering.

Only the host-side path behind `/data` changes. Compose, app configs, quality
profiles, watch history — all carry over untouched.

Things needing attention on the new host: the interface name and UUID in the
netplan/fstab templates, and the router's DHCP reservation for the new MAC.

### Planned: external disk with LVM

Not yet purchased. Intended shape: mains-powered 3.5" desktop external, 2–4 TB,
Windows disk set Offline and the whole physical disk passed through to the VM.
LVM on top (`vg mediavg`, `lv medialv`) + ext4, so a later disk is
`pvcreate` + `vgextend` + `lvextend -r`.

Migration: stack down, `rsync -aHvP`, verify hardlinks survived, repoint fstab
at the new UUID. Keep `nofail`.

**Verify hardlinks explicitly after the copy** — see
[Verifying hardlinks](#verifying-hardlinks). This is the one moment they can
break, and nothing in the continuous monitoring will tell you.

**`rsync -aHvP` copies `.mounted` along with everything else**, so the sentinel
lands on the new disk automatically. Confirm it before bringing the stack up.

**Clean up before migrating, not after.** `rsync -aH` preserves hardlinks that
already exist but never creates new ones, so unshared duplicates get copied
across verbatim and cost you twice.

**A linear VG spanning two external disks doubles the failure surface** — one
disk dies and the whole volume group goes, including data physically on the
survivor. Prefer one larger disk. LVM's value here is growing onto a
replacement, not spanning.

---

## Open / not implemented

- **Readarr + rreading-glasses**, replacing Chaptarr
- **MyAnonaMouse** membership — needs `mam_id` and the Dynamic Seedbox API call
  from inside gluetun's namespace
- **External disk purchase and `/data` migration**
- **Tailscale** — DNS baseline confirmed clean; `tailscaled` on the VM, never
  inside gluetun; no exit node; split DNS for `home.arpa` rather than a blanket
  MagicDNS takeover. Node sharing vs Funnel still open.
- **Android TV / Google TV device** for the Samsung TV
- **Proxmox mini-PC** — deferred, not imminent
- **`unhealthy` → Discord** watcher, and a dead-man's switch for qbit_manage
- **apprise digest pin**
