# Running Tunaria Reborn on a Linux server

This sets the server up as a systemd service: it starts at boot, restarts if it
crashes, and the admin panel's **Restart server** and **Update & restart**
buttons work. Commands are for Debian / Ubuntu; other distributions need the
equivalent packages.

Any small machine is enough: the server is plain Python and uses little CPU and
memory.

## 1. Install Python and git

```sh
sudo apt update
sudo apt install -y python3 git python3-psutil   # psutil is optional (CPU/memory in the panel)
python3 --version                                # needs 3.10 or newer
```

## 2. Create a service user and its folder

The server runs as its own unprivileged user. Its home folder holds the
GitHub key; the code goes in a `server/` folder underneath, so the key can
never end up in the git checkout.

```sh
sudo useradd --system --home-dir /opt/tunaria-reborn --create-home --shell /usr/sbin/nologin tunaria
```

## 3. Give the server read access to the GitHub repo

The repository is private, so the server needs a key to download it and its
updates. A **deploy key** is read-only and works for this one repository only.

```sh
sudo -H -u tunaria mkdir -p -m 700 /opt/tunaria-reborn/.ssh
sudo -H -u tunaria ssh-keygen -t ed25519 -N "" -C "tunaria-reborn server" -f /opt/tunaria-reborn/.ssh/id_ed25519
sudo cat /opt/tunaria-reborn/.ssh/id_ed25519.pub
```

On GitHub: repository **Settings → Deploy keys → Add deploy key**, paste the
line printed above, leave **Allow write access** unticked, and save.

(If you make the repository public instead, skip this step and clone with
`https://github.com/Guinnessstache/tunaria-reborn.git` below.)

## 4. Download the server

```sh
sudo -H -u tunaria git clone -b claude/serve-eqahosts \
    git@github.com:Guinnessstache/tunaria-reborn.git /opt/tunaria-reborn/server
```

The first connection asks you to confirm GitHub's host key; answer `yes`.
`-b claude/serve-eqahosts` is the branch with the current work; use `main` once
it has been merged there.

## 5. Configure it

```sh
sudo cp /opt/tunaria-reborn/server/deploy/tunaria-reborn.env.example /etc/tunaria-reborn.env
sudo nano /etc/tunaria-reborn.env
```

Set `--public-host` to the IP address players connect to: the server's public
IP for internet play, or its LAN IP for home use. The PS2/PCSX2 client's DNS
patch must point at the same address.

`--admin-host` decides who can reach the admin panel:

- `127.0.0.1` (default): only through an SSH tunnel (step 8). Safest.
- `0.0.0.0`: from anywhere that can reach port 8090. The panel is plain HTTP,
  so use a password you don't use anywhere else.

## 6. Install and start the service

```sh
sudo cp /opt/tunaria-reborn/server/deploy/tunaria-reborn.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now tunaria-reborn
sudo systemctl status tunaria-reborn
```

The first start generates an admin password. Read it from the log:

```sh
sudo journalctl -u tunaria-reborn | grep "Admin password"
```

You will be asked to choose your own at first sign-in. (Or set
`EQOA_ADMIN_PASSWORD` in `/etc/tunaria-reborn.env` to use a fixed one.)

## 7. Open the firewall

| Port | Protocol | Used for |
| --- | --- | --- |
| 9735 | TCP | login |
| 7000 | TCP | patch files and news (MOTD) |
| 10070 | UDP | the game |
| 8080 | TCP | status page |
| 8090 | TCP | admin panel (only if `--admin-host 0.0.0.0`) |

With `ufw`:

```sh
sudo ufw allow 9735/tcp
sudo ufw allow 7000/tcp
sudo ufw allow 10070/udp
sudo ufw allow 8080/tcp
sudo ufw allow 8090/tcp   # only if the admin panel should be reachable directly
```

Hosting at home? Forward the same ports on your router to the server.

## 8. Open the admin panel

- `--admin-host 0.0.0.0`: browse to `http://<server-ip>:8090/`.
- `--admin-host 127.0.0.1`: from your PC, open a tunnel and keep it running,
  then browse to http://localhost:8090/ on your PC:

  ```sh
  ssh -L 8090:localhost:8090 you@<server-ip>
  ```

## Day to day

| Task | How |
| --- | --- |
| Update | Admin panel → **Updates** → **Check for updates** → **Update & restart** |
| Restart | Admin panel → **Restart server**, or `sudo systemctl restart tunaria-reborn` |
| Stop / start | `sudo systemctl stop tunaria-reborn` / `sudo systemctl start tunaria-reborn` |
| Live log | Admin panel → **Logs**, or `sudo journalctl -u tunaria-reborn -f` |
| Change options | Edit `/etc/tunaria-reborn.env`, then `sudo systemctl restart tunaria-reborn` |
| Back up data | Admin panel → **Download data backup**, or copy `/opt/tunaria-reborn/server/data/` |
| Forgot admin password | `sudo systemctl stop tunaria-reborn`, delete `/opt/tunaria-reborn/server/data/admin.json`, start it again and read the new one from the log |

**Shut down server** in the admin panel stops the service until you start it
again with `systemctl start`; it does not restart by itself. Crashes and the
panel's restart buttons do restart it automatically.

## Before inviting other players

- **Game passwords are not checked yet**: anyone can log in to any account.
- The project is **AGPL-3.0**: if you run a modified server for other people,
  give them a way to get the source, for example a link in the message of the day.
