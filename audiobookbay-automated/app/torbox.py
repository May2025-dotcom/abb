"""
TorBox (debrid) integration: one account, configured with TORBOX_API_KEY.

The API key never leaves the server. Downloads are served by asking TorBox for
a short-lived download link and redirecting the browser to it.

Slow or dead torrents
---------------------
Every book sent to TorBox goes on a watch list. A background worker checks the
whole list every TORBOX_POLL_MINUTES (one API call), whether or not anyone has
the page open, and:
  - re-announces a torrent (asks trackers for peers again) when it has made no
    progress for TORBOX_REANNOUNCE_HOURS, and again after each such period
  - gives up after TORBOX_GIVE_UP_DAYS with no progress (0 = wait forever) and,
    if TORBOX_REMOVE_ON_GIVE_UP, deletes it from TorBox to free the slot
A slow download that keeps making progress is never given up on.

API reference: https://api.torbox.app  (v1)
  GET  /torrents/checkcached?hash=..&format=object&list_files=false
  POST /torrents/createtorrent        (form: magnet, seed, allow_zip, name)
  GET  /torrents/mylist?bypass_cache=true[&id=..]
  GET  /torrents/requestdl?token=..&torrent_id=..&file_id=..&zip_link=..
  POST /torrents/controltorrent       (json: torrent_id, operation)
"""

import os
import re
import threading
import time

import requests

import abb

API = "https://api.torbox.app/v1/api"
API_KEY = os.getenv("TORBOX_API_KEY", "").strip()
ENABLED = bool(API_KEY)


def _num_env(name, default):
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return default


POLL_MINUTES = _num_env("TORBOX_POLL_MINUTES", 10)
REANNOUNCE_HOURS = _num_env("TORBOX_REANNOUNCE_HOURS", 6)
GIVE_UP_DAYS = _num_env("TORBOX_GIVE_UP_DAYS", 7)
REMOVE_ON_GIVE_UP = os.getenv("TORBOX_REMOVE_ON_GIVE_UP", "true").lower() in ("1", "true", "yes")

_ID_NS = "torbox_id"      # hash -> TorBox torrent id
_WATCH_NS = "torbox_watch"  # hash -> watch record
_KEEP = 365 * 24 * 3600
ACTIVE = ("queued", "downloading")

_http = requests.Session()
_http.headers["User-Agent"] = "audiobookbay-automated"
_watch_lock = threading.RLock()


class TorBoxError(Exception):
    pass


def _call(method, path, **kwargs):
    if not ENABLED:
        raise TorBoxError("TorBox isn't set up (TORBOX_API_KEY is empty).")
    headers = {"Authorization": f"Bearer {API_KEY}"}
    try:
        resp = _http.request(method, f"{API}/{path}", headers=headers, timeout=20, **kwargs)
    except requests.RequestException as e:
        raise TorBoxError(f"Couldn't reach TorBox: {e}") from e
    try:
        body = resp.json()
    except ValueError:
        raise TorBoxError(f"TorBox returned HTTP {resp.status_code}") from None
    if resp.status_code >= 400 or not body.get("success", False):
        detail = body.get("detail") or body.get("error") or f"HTTP {resp.status_code}"
        raise TorBoxError(f"TorBox: {detail}")
    return body.get("data")


def infohash(magnet):
    m = re.search(r"btih:([0-9a-fA-F]{40}|[A-Za-z2-7]{32})", magnet or "")
    if not m:
        raise TorBoxError("Couldn't read the infohash from the magnet link.")
    return m.group(1).lower()


# --- Watch list ----------------------------------------------------------------


def _get(h):
    return abb.store.get(_WATCH_NS, h)


def _save(rec):
    abb.store.set(_WATCH_NS, rec["hash"], rec, ttl=_KEEP)


def watch_list():
    """All watched books, newest first."""
    return sorted((r for _, r in abb.store.items(_WATCH_NS)), key=lambda r: -r.get("added_at", 0))


def _forget_id(h):
    abb.store.delete(_ID_NS, h)


# --- Reading TorBox's view of a torrent ----------------------------------------


def _files(raw):
    out = []
    for f in raw or []:
        name = f.get("short_name") or f.get("name") or "file"
        out.append({"id": f.get("id"), "name": name.split("/")[-1], "size": f.get("size") or 0})
    return out


def _snapshot(t):
    """Normalise one torrent from mylist."""
    present = t.get("download_present")
    ready = bool(present or (t.get("download_finished") and present is not False))
    state_text = t.get("download_state") or ""
    seeds = int(t.get("seeds") or 0)
    speed = int(t.get("download_speed") or 0)
    low = state_text.lower()
    return {
        "ready": ready,
        "error": "error" in low or "failed" in low,
        "stalled": "stalled" in low or (not ready and seeds == 0 and speed == 0),
        "progress": round(float(t.get("progress") or 0) * 100, 1),
        "seeds": seeds,
        "peers": int(t.get("peers") or 0),
        "speed": speed,
        "eta": t.get("eta") or 0,
        "detail": state_text,
        "files": _files(t.get("files")) if ready else [],
    }


def _apply(rec, snap, now):
    """Update a watch record from a TorBox snapshot. Returns True if it changed state."""
    before = rec.get("state")
    rec.update({k: snap[k] for k in ("progress", "seeds", "peers", "speed", "eta", "detail", "stalled")})
    if snap["ready"]:
        rec["state"] = "ready"
        rec.setdefault("finished_at", now)
        rec["files"] = snap["files"]
    elif snap["error"]:
        rec["state"] = "error"
        rec["message"] = f"TorBox: {snap['detail']}"
    else:
        rec["state"] = "downloading"
        if snap["progress"] > rec.get("last_progress", 0) + 0.05:
            rec["last_progress"] = snap["progress"]
            rec["last_progress_at"] = now
    return rec["state"] != before


def _public(rec):
    """What the browser gets for one book."""
    now = time.time()
    out = {k: rec.get(k) for k in (
        "hash", "title", "link", "state", "progress", "seeds", "peers", "speed", "eta",
        "detail", "stalled", "files", "message", "downloaded_at",
    )}
    out["waiting_seconds"] = max(0, int(now - rec.get("added_at", now)))
    out["idle_seconds"] = max(0, int(now - rec.get("last_progress_at", rec.get("added_at", now))))
    out["reannounced_seconds_ago"] = (
        max(0, int(now - rec["last_reannounce_at"])) if rec.get("last_reannounce_at") else None
    )
    if rec["state"] == "gave_up":
        out["message"] = (
            f"Gave up after {GIVE_UP_DAYS:g} days without progress"
            + (" and removed it from TorBox." if rec.get("removed_from_torbox") else ".")
        )
    return out


# --- Status --------------------------------------------------------------------


def cached_hashes(hashes):
    """Return the subset of hashes TorBox has cached (one API call)."""
    if not hashes:
        return set()
    data = _call(
        "GET", "torrents/checkcached",
        params={"hash": ",".join(hashes), "format": "object", "list_files": "false"},
    )
    return {h.lower() for h in (data or {}).keys()} if isinstance(data, dict) else set()


def _torrent(torrent_id):
    data = _call("GET", "torrents/mylist", params={"id": torrent_id, "bypass_cache": "true"})
    if isinstance(data, list):
        data = next((t for t in data if t.get("id") == torrent_id), None)
    return data


def status(h):
    """One book: its watch record if we sent it to TorBox, else cached / not_cached."""
    with _watch_lock:
        rec = _get(h)
        if rec and rec["state"] in ACTIVE + ("ready", "error"):
            tid = abb.store.get(_ID_NS, h)
            if tid is not None:
                t = _torrent(tid)
                if t:
                    _apply(rec, _snapshot(t), time.time())
                    _save(rec)
                elif rec["state"] != "queued":
                    rec["state"] = "removed"
                    rec["message"] = "It's no longer in your TorBox account."
                    _forget_id(h)
                    _save(rec)
        if rec:
            return {**_public(rec), "torrent_id": abb.store.get(_ID_NS, h)}
    return {"state": "cached" if h in cached_hashes([h]) else "not_cached"}


def statuses(hashes):
    """Batch version for the results page: one cache check for all unwatched hashes."""
    out, unknown = {}, []
    for h in hashes:
        rec = _get(h)
        if rec:
            out[h] = _public(rec)  # kept fresh by the worker; no API call per book
        else:
            unknown.append(h)
    cached = cached_hashes(unknown)
    for h in unknown:
        out[h] = {"state": "cached" if h in cached else "not_cached"}
    return out


# --- Actions -------------------------------------------------------------------


def add(magnet, name=None, link=None):
    """Send a book to TorBox (instant if cached) and start watching it."""
    h = infohash(magnet)
    with _watch_lock:
        rec = _get(h)
        if rec and rec["state"] in ACTIVE + ("ready",) and abb.store.get(_ID_NS, h) is not None:
            return status(h)
        form = {"magnet": magnet, "seed": "3", "allow_zip": "true"}  # seed 3 = don't seed
        if name:
            form["name"] = name
        data = _call("POST", "torrents/createtorrent", data=form) or {}
        tid = data.get("torrent_id") or data.get("id")
        now = time.time()
        rec = {
            "hash": h, "magnet": magnet, "title": name or (rec or {}).get("title") or h,
            "link": link or (rec or {}).get("link"), "added_at": now, "last_progress_at": now,
            "last_progress": 0, "state": "downloading" if tid is not None else "queued",
            "progress": 0, "seeds": 0, "peers": 0, "speed": 0, "eta": 0, "detail": "",
            "message": "Waiting for a free TorBox slot." if tid is None else None,
        }
        if tid is not None:
            abb.store.set(_ID_NS, h, tid, ttl=_KEEP)
        _save(rec)
    return status(h)


def control(torrent_id, operation):
    return _call("POST", "torrents/controltorrent", json={"torrent_id": torrent_id, "operation": operation})


def remove(h):
    """Delete from TorBox (if it's there) and from the watch list."""
    with _watch_lock:
        tid = abb.store.get(_ID_NS, h)
        if tid is not None:
            try:
                control(tid, "delete")
            except TorBoxError as e:
                if "not found" not in str(e).lower():
                    raise
        _forget_id(h)
        abb.store.delete(_WATCH_NS, h)


def retry(h):
    """Start a book again from scratch (after giving up, an error, or removal)."""
    rec = _get(h)
    if not rec:
        raise TorBoxError("That book isn't on the TorBox list any more.")
    magnet, title, link = rec["magnet"], rec.get("title"), rec.get("link")
    remove(h)
    return add(magnet, name=title, link=link)


def mark_downloaded(h):
    with _watch_lock:
        rec = _get(h)
        if rec:
            rec["downloaded_at"] = time.time()
            _save(rec)


def download_url(magnet, file_id=None, wait_seconds=20, link=None, name=None):
    """Make sure the book is in TorBox and ready, then get a download link.

    file_id=None gives a zip of the whole book.
    """
    h = infohash(magnet)
    st = add(magnet, name=name, link=link)
    if st["state"] == "downloading" and h in cached_hashes([h]):
        # A cached torrent becomes ready within seconds of being added; an
        # uncached one can take days, so only wait in the cached case.
        deadline = time.monotonic() + wait_seconds
        while st["state"] == "downloading" and time.monotonic() < deadline:
            time.sleep(2)
            st = status(h)
    if st["state"] != "ready":
        raise TorBoxError("Not ready yet - TorBox is still downloading this book.")
    params = {"token": API_KEY, "torrent_id": st["torrent_id"], "redirect": "false"}
    if file_id is None:
        params["zip_link"] = "true"
    else:
        params["file_id"] = file_id
    url = _call("GET", "torrents/requestdl", params=params)
    if not isinstance(url, str) or not url.startswith("http"):
        raise TorBoxError("TorBox didn't return a download link.")
    mark_downloaded(h)
    return url


# --- Background worker -----------------------------------------------------------


def poll_once(now=None):
    """Check every waiting book with one API call; re-announce or give up as needed."""
    now = now or time.time()
    with _watch_lock:
        waiting = [r for r in watch_list() if r["state"] in ACTIVE]
        if not waiting:
            return {"checked": 0}
        data = _call("GET", "torrents/mylist", params={"bypass_cache": "true", "limit": 1000}) or []
        by_hash = {str(t.get("hash", "")).lower(): t for t in (data if isinstance(data, list) else [data])}
        summary = {"checked": len(waiting), "ready": 0, "reannounced": 0, "gave_up": 0}

        for rec in waiting:
            h = rec["hash"]
            t = by_hash.get(h)
            if t is None:
                if rec["state"] == "queued":
                    continue  # still waiting for a free slot
                rec["state"] = "removed"
                rec["message"] = "It's no longer in your TorBox account."
                _forget_id(h)
                _save(rec)
                continue

            abb.store.set(_ID_NS, h, t["id"], ttl=_KEEP)
            if rec["state"] == "queued":
                rec["last_progress_at"] = now  # the clock starts when it actually starts
            _apply(rec, _snapshot(t), now)
            if rec["state"] == "ready":
                summary["ready"] += 1
            elif rec["state"] == "downloading":
                idle = now - rec.get("last_progress_at", rec["added_at"])
                since_nudge = now - rec.get("last_reannounce_at", 0)
                if GIVE_UP_DAYS and idle >= GIVE_UP_DAYS * 86400:
                    rec["state"] = "gave_up"
                    rec["gave_up_at"] = now
                    if REMOVE_ON_GIVE_UP:
                        try:
                            control(t["id"], "delete")
                            rec["removed_from_torbox"] = True
                            _forget_id(h)
                        except TorBoxError as e:
                            print(f"[WARN] TorBox: couldn't remove {rec.get('title')}: {e}")
                    summary["gave_up"] += 1
                    print(f"TorBox: gave up on '{rec.get('title')}' after {idle / 86400:.1f} days without progress")
                elif REANNOUNCE_HOURS and idle >= REANNOUNCE_HOURS * 3600 and since_nudge >= REANNOUNCE_HOURS * 3600:
                    try:
                        control(t["id"], "reannounce")
                        rec["last_reannounce_at"] = now
                        summary["reannounced"] += 1
                    except TorBoxError as e:
                        print(f"[WARN] TorBox: re-announce failed for {rec.get('title')}: {e}")
            _save(rec)
    return summary


_worker_started = False


def start_worker():
    global _worker_started
    if not ENABLED or POLL_MINUTES <= 0 or _worker_started:
        return
    _worker_started = True

    def loop():
        while True:
            time.sleep(POLL_MINUTES * 60)
            try:
                result = poll_once()
                if result.get("checked"):
                    print(f"TorBox check: {result}", flush=True)
            except Exception as e:  # keep going; TorBox may just be down
                print(f"[WARN] TorBox check failed: {e}", flush=True)

    threading.Thread(target=loop, daemon=True, name="torbox-watch").start()
    print(f"TorBox: checking waiting books every {POLL_MINUTES:g} min; re-announce after "
          f"{REANNOUNCE_HOURS:g} h idle; give up after "
          + (f"{GIVE_UP_DAYS:g} days idle" if GIVE_UP_DAYS else "never"))
