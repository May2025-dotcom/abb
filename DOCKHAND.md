# Running on a server with Dockhand

The server uses `docker-compose.server.yaml`. It pulls the image GitHub builds
from this repo, and takes every setting from Dockhand, so no secrets live in git.

## 1. Check the image exists

Every push to `main` runs **Actions → Build and Push Multi-Architecture Image**,
which publishes:

```
ghcr.io/<your-github-username>/audiobookbay-automated:latest   (all lowercase)
```

Once it has run, it shows under **Packages** in the repo's sidebar.

The image is private if the repo is. Either:
- **make the package public** (it holds only app code, no settings):
  package page → Package settings → Change visibility → Public, or
- **add ghcr.io as a registry in Dockhand** with your GitHub username and a
  classic personal access token that has only `read:packages`.

## 2. Create the stack in Dockhand

**Stacks → From Git**, then:

| Field | Value |
|---|---|
| Repository | this repo (for a private repo: a GitHub token with `repo` read access, or an SSH deploy key) |
| Branch | `main` |
| Compose path | `docker-compose.server.yaml` |

## 3. Environment variables (in Dockhand, not in git)

Required:

| Variable | Value |
|---|---|
| `APP_IMAGE` | `ghcr.io/<your-github-username>/audiobookbay-automated:latest` |
| `APP_PASSWORD` | a long password for the web page |
| `OPENVPN_USER` | NordVPN service username |
| `OPENVPN_PASSWORD` | NordVPN service password |

NordVPN service credentials: NordVPN dashboard → Set up NordVPN manually →
Service credentials (not your NordVPN email/password).

Optional (defaults shown):

| Variable | Default | |
|---|---|---|
| `SERVER_COUNTRIES` | `United Kingdom` | NordVPN country |
| `ABB_HOSTNAME` | `audiobookbay.lu` | |
| `APP_PORT` | `5078` | port on the server |
| `COVERS` | `proxy` | `proxy`, `direct` or `off` |
| `ABB_REQUEST_DELAY` / `ABB_REQUEST_JITTER` | `2` / `3` | pause between requests |
| `ABB_COOLDOWN` | `300` | pause after the site pushes back |
| `COOKIE_SECURE` | `false` | set `true` once served over HTTPS |
| `TRUST_PROXY` | `false` | set `true` when behind a reverse proxy |
| `TORBOX_API_KEY` | empty | TorBox API key (torbox.app → Settings → API); shows the TorBox buttons |
| `TORBOX_POLL_MINUTES` | `10` | how often waiting TorBox books are checked (0 = only on demand) |
| `TORBOX_REANNOUNCE_HOURS` | `6` | re-announce a book after this long without progress |
| `TORBOX_GIVE_UP_DAYS` | `7` | give up after this many days without progress (`0` = never) |
| `TORBOX_REMOVE_ON_GIVE_UP` | `true` | delete given-up books from TorBox to free the slot |
| `NAV_LINK_NAME` / `NAV_LINK_URL` | empty | extra link in the top bar |

The deploy fails with a clear message if a required variable is missing.

## 4. Deploy and check

Deploy, then in the logs of:
- `abb-vpn`: `Initialization Sequence Completed` and a public IP line
- `audiobookbay-automated`: `Login: on` and
  `AudioBook Bay traffic exits from IP: ...` (the NordVPN IP, not the server's)

Open `http://<server-ip>:5078` and log in.

## 5. HTTPS (recommended)

The login page is served over plain HTTP, so the password crosses the internet
unencrypted. Put the app behind a reverse proxy with HTTPS (Caddy, Nginx Proxy
Manager, Traefik...) and then:
- set `COOKIE_SECURE=true` and `TRUST_PROXY=true`
- set `APP_PORT` to something only the proxy uses, or remove the port and put
  the proxy on the same Docker network

## Updating

Push to `main`, wait for the Action to finish, then redeploy the stack in
Dockhand (or enable its auto-update / webhook). Caches and the login key live in
the `abb-data` volume and survive redeploys.
