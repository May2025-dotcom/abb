"""
Everything that talks to AudioBook Bay goes through this module, so it can be
polite and hard to tell apart from a person browsing:

- Connects like Chrome (TLS fingerprint, HTTP/2, headers) via curl_cffi.
- One request at a time, with a random human-like pause between requests.
- Sends a Referer like a browser clicking through the site.
- When the site pushes back (403/429/503), stops ALL requests for a while.
- Caches searches, magnet links and covers on disk, and remembers the pause,
  so restarting the container doesn't re-fetch everything or skip the pause.
"""

import hashlib
import json
import os
import random
import sqlite3
import threading
import time

try:
    from curl_cffi import requests as http
    from curl_cffi.requests.exceptions import RequestException as RequestError

    IMPERSONATE = {"impersonate": "chrome"}
except ImportError:  # still works without it, just less browser-like
    import requests as http
    from requests.exceptions import RequestException as RequestError

    IMPERSONATE = {}
    print("[WARN] curl_cffi not installed; requests won't look like a browser")


class RateLimited(Exception):
    pass


def _float_env(name, default):
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return default


# Pause between requests: a random time between MIN and MIN + JITTER seconds.
REQUEST_DELAY = _float_env("ABB_REQUEST_DELAY", 2.0)
REQUEST_JITTER = _float_env("ABB_REQUEST_JITTER", 3.0)
COOLDOWN_SECONDS = int(_float_env("ABB_COOLDOWN", 300))

# Optional proxy for ALL AudioBook Bay traffic, e.g. http://gluetun:8888 or
# socks5h://user:pass@host:1080. If it's set but unreachable, requests fail:
# there is deliberately no fallback to a direct connection.
PROXY = os.getenv("ABB_PROXY", "").strip()
PROXIES = {"http": PROXY, "https": PROXY} if PROXY else None


class ProxyUnavailable(Exception):
    pass

DATA_DIR = os.getenv("DATA_DIR", "/data" if os.path.isdir("/data") else "./data")
os.makedirs(os.path.join(DATA_DIR, "covers"), exist_ok=True)


# --- Persistent store (survives restarts) ------------------------------------


class Store:
    """Small key/value store in SQLite with expiry."""

    def __init__(self, path):
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS kv (ns TEXT, k TEXT, expires REAL, v TEXT, PRIMARY KEY (ns, k))"
        )
        self._db.execute("DELETE FROM kv WHERE expires < ?", (time.time(),))
        self._db.commit()

    def get(self, ns, key):
        with self._lock:
            row = self._db.execute(
                "SELECT v FROM kv WHERE ns=? AND k=? AND expires>?", (ns, key, time.time())
            ).fetchone()
        return json.loads(row[0]) if row else None

    def items(self, ns):
        """All unexpired (key, value) pairs in a namespace."""
        with self._lock:
            rows = self._db.execute(
                "SELECT k, v FROM kv WHERE ns=? AND expires>?", (ns, time.time())
            ).fetchall()
        return [(k, json.loads(v)) for k, v in rows]

    def delete(self, ns, key):
        with self._lock:
            self._db.execute("DELETE FROM kv WHERE ns=? AND k=?", (ns, key))
            self._db.commit()

    def set(self, ns, key, value, ttl):
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO kv VALUES (?, ?, ?, ?)",
                (ns, key, time.time() + ttl, json.dumps(value)),
            )
            self._db.commit()


store = Store(os.path.join(DATA_DIR, "cache.sqlite3"))

SEARCH_TTL = 30 * 60
MAGNET_TTL = 30 * 24 * 3600  # an infohash never changes
COVER_URL_TTL = 30 * 24 * 3600


# --- The gate: one polite request at a time ----------------------------------

if IMPERSONATE:
    from curl_cffi import CurlOpt

    # Only ABB_PROXY decides routing. An empty NOPROXY stops libcurl reading a
    # NO_PROXY environment variable that could silently bypass the VPN.
    _session = http.Session(curl_options={CurlOpt.NOPROXY: ""})
else:
    _session = http.Session()
    _session.trust_env = False  # requests: ignore HTTP(S)_PROXY / NO_PROXY

# Pages: one at a time with a human-like pause. Images: their own quick queue,
# like a browser loading a page's pictures, so covers never hold up clicks.
_gates = {
    "page": {"lock": threading.Lock(), "last": 0.0, "delay": (REQUEST_DELAY, REQUEST_JITTER)},
    "image": {"lock": threading.Lock(), "last": 0.0, "delay": (0.2, 0.6)},
}


def cooldown_remaining():
    until = store.get("state", "cooldown_until") or 0
    return max(0.0, until - time.time())


def _start_cooldown(seconds, why):
    store.set("state", "cooldown_until", time.time() + seconds, ttl=seconds + 60)
    print(f"[WARN] {why}; pausing all AudioBook Bay requests for {seconds}s")


def get(url, referer=None, kind="page"):
    """Fetch a URL from AudioBook Bay politely. Raises RateLimited when paused."""
    gate = _gates["image" if kind == "image" else "page"]
    with gate["lock"]:
        wait = cooldown_remaining()
        if wait:
            raise RateLimited(
                f"AudioBook Bay is rate-limiting this server. Try again in {int(wait) + 1}s."
            )

        base, jitter = gate["delay"]
        gap = base + random.uniform(0, jitter) - (time.monotonic() - gate["last"])
        if gap > 0:
            time.sleep(gap)

        headers = {}
        if referer:
            # What Chrome sends when you click a link within the same site.
            headers["Referer"] = referer
            headers["Sec-Fetch-Site"] = "same-origin"
        if kind == "image":
            headers.update(
                {
                    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
                    "Sec-Fetch-Dest": "image",
                    "Sec-Fetch-Mode": "no-cors",
                }
            )
        try:
            resp = _session.get(url, headers=headers, timeout=20, proxies=PROXIES, **IMPERSONATE)
        except RequestError as e:
            if PROXY and _looks_like_proxy_failure(e):
                raise ProxyUnavailable(
                    "Can't reach the VPN proxy, so nothing was sent to AudioBook Bay. "
                    "Check the VPN container is running and connected."
                ) from e
            raise
        finally:
            gate["last"] = time.monotonic()

        if resp.status_code in (403, 429, 503):
            retry_after = resp.headers.get("Retry-After", "")
            seconds = int(retry_after) if retry_after.isdigit() else COOLDOWN_SECONDS
            _start_cooldown(seconds, f"AudioBook Bay returned HTTP {resp.status_code}")
            raise RateLimited(
                f"AudioBook Bay is rate-limiting this server (HTTP {resp.status_code}). "
                f"Pausing for {seconds}s."
            )
        resp.raise_for_status()
        return resp


def _looks_like_proxy_failure(err):
    text = str(err).lower()
    return any(w in text for w in ("proxy", "connect to", "resolve", "refused", "tunnel"))


def describe_exit_ip():
    """Return the public IP AudioBook Bay traffic comes from (for the startup log)."""
    try:
        resp = _session.get("https://api.ipify.org", timeout=15, proxies=PROXIES, **IMPERSONATE)
        return resp.text.strip()
    except Exception as e:
        return f"unknown ({e})"


# --- Covers --------------------------------------------------------------------


IMAGE_SIGNATURES = (b"\xff\xd8\xff", b"\x89PNG", b"GIF8", b"RIFF")


def cover_id(url):
    """Register a cover URL seen in search results and return an opaque id for it."""
    cid = hashlib.sha1(url.encode()).hexdigest()[:20]
    store.set("cover_url", cid, url, ttl=COVER_URL_TTL)
    return cid


def cover_file(cid, referer):
    """Return a path to the cached cover, downloading it once if needed."""
    if not cid.isalnum():
        return None
    path = os.path.join(DATA_DIR, "covers", cid)
    if os.path.exists(path):
        return path
    url = store.get("cover_url", cid)
    if not url:
        return None  # only fetch URLs that came from real search results
    resp = get(url, referer=referer, kind="image")
    if not resp.content.startswith(IMAGE_SIGNATURES):
        raise ValueError("cover URL did not return an image")
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(resp.content)
    os.replace(tmp, path)
    return path
