import os
import re
import threading
import time
import unicodedata
from io import BytesIO
from urllib.parse import quote, quote_plus, urljoin, urlparse

from bs4 import BeautifulSoup
from dotenv import load_dotenv
from flask import Flask, abort, jsonify, redirect, render_template, request, send_file, url_for

import abb
from abb import RateLimited
from auth import init_auth
import torbox
from clients import get_client

app = Flask(__name__)
load_dotenv()
init_auth(app, abb.DATA_DIR)

# --- Configuration -----------------------------------------------------------

ABB_HOSTNAME = os.getenv("ABB_HOSTNAME", "audiobookbay.lu").strip("'\"")
PAGE_LIMIT = int(os.getenv("PAGE_LIMIT", 5))
# Covers: "proxy" (server fetches and caches them), "direct" (browser loads
# them from the source), or "off".
COVERS = os.getenv("COVERS", "proxy").strip().lower()

DOWNLOAD_CLIENT = os.getenv("DOWNLOAD_CLIENT")
DL_URL = os.getenv("DL_URL")
if DL_URL:
    _parsed = urlparse(DL_URL)
    DL_SCHEME, DL_HOST, DL_PORT = _parsed.scheme, _parsed.hostname, _parsed.port
else:
    DL_SCHEME = os.getenv("DL_SCHEME", "http")
    DL_HOST = os.getenv("DL_HOST")
    DL_PORT = os.getenv("DL_PORT")
    if DL_HOST and DL_PORT:
        DL_URL = f"{DL_SCHEME}://{DL_HOST}:{DL_PORT}"

SAVE_PATH_BASE = os.getenv("SAVE_PATH_BASE")
NAV_LINK_NAME = os.getenv("NAV_LINK_NAME")
NAV_LINK_URL = os.getenv("NAV_LINK_URL")
FLASK_PORT = int(os.getenv("PORT", 5078))

client = get_client(
    DOWNLOAD_CLIENT,
    {
        "scheme": DL_SCHEME,
        "host": DL_HOST,
        "port": DL_PORT,
        "url": DL_URL,
        "username": os.getenv("DL_USERNAME"),
        "password": os.getenv("DL_PASSWORD"),
        "category": os.getenv("DL_CATEGORY", "Audiobookbay-Audiobooks"),
        "save_path_base": SAVE_PATH_BASE,
    },
)

print(f"ABB_HOSTNAME: {ABB_HOSTNAME}")
print(f"DOWNLOAD_CLIENT: {DOWNLOAD_CLIENT or 'none (direct magnet mode)'}")
if client:
    print(f"DL_URL: {DL_URL}")
    print(f"SAVE_PATH_BASE: {SAVE_PATH_BASE}")
print(f"PAGE_LIMIT: {PAGE_LIMIT}")
print(f"Request pause: {abb.REQUEST_DELAY}-{abb.REQUEST_DELAY + abb.REQUEST_JITTER}s, covers: {COVERS}, data: {abb.DATA_DIR}")
print(f"TorBox: {'on' if torbox.ENABLED else 'off (no TORBOX_API_KEY)'}")
print(f"ABB_PROXY: {re.sub(r'//[^@/]*@', '//***@', abb.PROXY) or 'none (direct connection)'}")


def _log_exit_ip():
    # Runs in the background so a slow or down VPN doesn't delay startup, and
    # retries for ~2 minutes while the VPN container is still connecting.
    for _ in range(12):
        ip = abb.describe_exit_ip()
        if not ip.startswith("unknown"):
            break
        time.sleep(10)
    note = "" if abb.PROXY else " (your own connection - no proxy set)"
    print(f"AudioBook Bay traffic exits from IP: {ip}{note}", flush=True)


threading.Thread(target=_log_exit_ip, daemon=True).start()
print(f"PORT: {FLASK_PORT}")

DEFAULT_TRACKERS = [
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.demonii.com:1337/announce",
    "udp://tracker.openbittorrent.com:80",
    "udp://exodus.desync.com:6969/announce",
    "udp://tracker.torrent.eu.org:451/announce",
]


def _torbox_ready_count():
    """Books ready in TorBox that haven't been downloaded yet (for the menu badge)."""
    if not torbox.ENABLED:
        return 0
    return sum(1 for r in torbox.watch_list() if r["state"] == "ready" and not r.get("downloaded_at"))


@app.context_processor
def inject_globals():
    return {
        "nav_link_name": NAV_LINK_NAME,
        "nav_link_url": NAV_LINK_URL,
        "has_client": client is not None,
        "has_torbox": torbox.ENABLED,
        "torbox_ready_count": _torbox_ready_count(),
    }


# --- Scraping ----------------------------------------------------------------


def _match(pattern, text):
    m = re.search(pattern, text, re.DOTALL)
    return m.group(1).strip() if m else "N/A"


def _parse_post(post):
    title_el = post.select_one(".postTitle > h2 > a")
    if not title_el:
        return None

    img = post.select_one("img")
    info = post.select_one(".postInfo")
    info_text = info.get_text(separator=" ", strip=True) if info else ""

    book = {
        "title": title_el.text.strip(),
        "link": f"https://{ABB_HOSTNAME}{title_el['href']}",
        "cover": urljoin(f"https://{ABB_HOSTNAME}/", img["src"]) if img and img.get("src") else None,
        "language": _match(r"Language:\s*(.*?)(?:\s*Keywords:|$)", info_text),
        "post_date": "N/A",
        "format": "N/A",
        "bitrate": "N/A",
        "file_size": "N/A",
    }

    details = post.select_one(".postContent p[style*='text-align:center']")
    if details:
        html = str(details)
        book["post_date"] = _match(r"Posted:\s*([^<]+)", html)
        book["format"] = _match(r"Format:\s*<span[^>]*>([^<]+)</span>", html)
        book["bitrate"] = _match(r"Bitrate:\s*<span[^>]*>([^<]+)</span>", html)
        size = re.search(r"File Size:\s*<span[^>]*>([^<]+)</span>\s*([^<]+)", html)
        if size:
            book["file_size"] = f"{size.group(1).strip()} {size.group(2).strip()}"
    return book


def _search_url(query, page):
    q = quote_plus(query.lower())
    # Page 1 is what the site's own search box produces.
    if page == 1:
        return f"https://{ABB_HOSTNAME}/?s={q}"
    return f"https://{ABB_HOSTNAME}/page/{page}/?s={q}"


def _fetch_page(query, page):
    """Return (books, has_next_page) for one page of search results."""
    # Referer: like a browser, page 1 comes from the home page, page N from page N-1.
    referer = f"https://{ABB_HOSTNAME}/" if page == 1 else _search_url(query, page - 1)
    resp = abb.get(_search_url(query, page), referer=referer)
    books = []
    for post in BeautifulSoup(resp.text, "html.parser").select(".post"):
        try:
            book = _parse_post(post)
            if book:
                books.append(book)
        except Exception as e:  # one broken post shouldn't kill the search
            print(f"[ERROR] Could not parse a post: {e}")
    has_next = f"/page/{page + 1}/" in resp.text
    return books, has_next


def search_audiobookbay(query, max_pages=PAGE_LIMIT):
    """Fetch result pages one at a time, stopping when there's no next page."""
    key = query.strip().lower()
    cached = abb.store.get("search", key)
    if cached is not None:
        print(f"Search '{query}' served from cache")
        return cached

    results = []
    for page in range(1, max_pages + 1):
        try:
            books, has_next = _fetch_page(query, page)
        except RateLimited:
            if results:  # keep what we already have rather than failing outright
                print("[WARN] Rate limited mid-search; returning partial results")
                break
            raise
        except abb.RequestError as e:
            print(f"[ERROR] Page {page} failed: {e}")
            if results:
                break
            raise
        results.extend(books)
        if not books or not has_next:
            break

    print(f"Search '{query}': {len(results)} results from {page} page(s)")
    abb.store.set("search", key, results, ttl=abb.SEARCH_TTL)
    return results


def extract_magnet_link(details_url):
    cached = abb.store.get("magnet", details_url)
    if cached:
        return cached
    html = abb.get(details_url, referer=f"https://{ABB_HOSTNAME}/").text
    magnet = _build_magnet(html)
    abb.store.set("magnet", details_url, magnet, ttl=abb.MAGNET_TTL)
    return magnet


def _build_magnet(html):
    soup = BeautifulSoup(html, "html.parser")

    hash_cell = soup.find("td", string=re.compile(r"Info Hash", re.IGNORECASE))
    if not hash_cell:
        raise ValueError("Info hash not found on the details page")
    info_hash = hash_cell.find_next_sibling("td").text.strip()

    trackers = [
        td.text.strip()
        for td in soup.find_all("td", string=re.compile(r"udp://|https?://", re.IGNORECASE))
    ] or DEFAULT_TRACKERS

    title = soup.select_one(".postTitle h1")
    name = f"&dn={quote(title.text.strip())}" if title else ""
    trackers_q = "".join(f"&tr={quote(t, safe='')}" for t in trackers)
    return f"magnet:?xt=urn:btih:{info_hash}{name}{trackers_q}"


def _valid_details_link(link):
    """Only fetch pages on the configured AudioBook Bay host."""
    try:
        parsed = urlparse(link)
    except ValueError:
        return False
    return parsed.scheme == "https" and parsed.hostname == ABB_HOSTNAME


_RESERVED_NAMES = re.compile(r"^(con|prn|aux|nul|com[0-9¹²³]|lpt[0-9¹²³])$", re.IGNORECASE)
MAX_NAME_BYTES = 200  # under the 255-byte limit, leaves room for long parent paths


def safe_filename(title, fallback="audiobook"):
    """Make a file/folder name that is valid on Windows, macOS and Linux."""
    name = unicodedata.normalize("NFC", title or "")
    name = re.sub(r"[\t\n\r\v\f]", " ", name)
    # Control and invisible formatting characters (incl. bidi overrides).
    name = "".join(ch for ch in name if unicodedata.category(ch) not in ("Cc", "Cf"))
    # Keep "Title: Subtitle" and "A/B" readable rather than deleting the separator.
    name = re.sub(r"\s*:\s*", " - ", name)
    name = re.sub(r"[\\/|]", "-", name)
    # Remaining characters Windows forbids.
    name = re.sub(r'[*?"<>]', "", name)
    name = re.sub(r"\s+", " ", name)
    # No leading dots (hidden files) or dashes (look like command options),
    # no trailing dots or spaces (Windows drops them).
    name = name.strip(" .-")

    # Trim to a byte budget without cutting a character in half.
    if len(name.encode("utf-8")) > MAX_NAME_BYTES:
        name = name.encode("utf-8")[:MAX_NAME_BYTES].decode("utf-8", "ignore").rstrip(" .-")

    if not name:
        name = fallback
    # Windows device names are invalid even with an extension ("CON.mp3").
    if _RESERVED_NAMES.match(name.split(".")[0].strip()):
        name = f"_{name}"
    return name


# --- Routes ------------------------------------------------------------------


@app.route("/", methods=["GET", "POST"])
def search():
    query = request.form.get("query", "").strip() if request.method == "POST" else ""
    books, error = [], None
    if query:
        try:
            books = search_audiobookbay(query)
        except RateLimited as e:
            error = str(e)
        except Exception as e:
            print(f"[ERROR] Search failed: {e}")
            error = f"Search failed: {e}"
    # Don't modify cached results in place; build what the page shows.
    books = [dict(b, cover=_cover_src(b.get("cover"))) for b in books]
    return render_template("search.html", books=books, query=query, error=error,
                           searched=bool(query))


def _cover_src(url):
    if not url or COVERS == "off":
        return None
    if COVERS == "direct":
        return url
    return url_for("cover", cid=abb.cover_id(url))


_IMAGE_TYPES = [(b"\xff\xd8\xff", "image/jpeg"), (b"\x89PNG", "image/png"),
                (b"GIF8", "image/gif"), (b"RIFF", "image/webp")]


@app.route("/cover/<cid>")
def cover(cid):
    """Serve a cover from the on-disk cache, fetching it politely the first time."""
    fallback = url_for("static", filename="images/default_cover.jpg")
    try:
        path = abb.cover_file(cid, referer=f"https://{ABB_HOSTNAME}/")
    except RateLimited:
        return redirect(fallback)
    except Exception as e:
        print(f"[WARN] Cover {cid} failed: {e}")
        return redirect(fallback)
    if not path:
        return redirect(fallback)
    with open(path, "rb") as f:
        head = f.read(12)
    mimetype = next((m for sig, m in _IMAGE_TYPES if head.startswith(sig)), None)
    if not mimetype:  # not an image (e.g. an error page) - don't serve it
        return redirect(fallback)
    return send_file(path, mimetype=mimetype, max_age=30 * 24 * 3600)


@app.route("/magnet", methods=["POST"])
def magnet():
    link = (request.json or {}).get("link", "")
    if not _valid_details_link(link):
        return jsonify({"message": "Invalid link"}), 400
    try:
        return jsonify({"magnet": extract_magnet_link(link)})
    except RateLimited as e:
        return jsonify({"message": str(e)}), 429
    except Exception as e:
        return jsonify({"message": f"Couldn't get magnet link: {e}"}), 502


@app.route("/magnet-file")
def magnet_file():
    """Serve the magnet link as a .magnet file for qBittorrent watched folders.

    Served as a plain link download (not generated in the browser) so every
    click counts as a user download and browsers don't block the 2nd, 3rd...
    """
    link, title = request.args.get("link", ""), request.args.get("title", "")
    if not _valid_details_link(link):
        abort(400)
    try:
        magnet = extract_magnet_link(link)
    except RateLimited as e:
        return render_template("error.html", message=str(e)), 429
    except Exception as e:
        return render_template("error.html", message=f"Couldn't get magnet link: {e}"), 502
    return send_file(
        BytesIO((magnet + "\n").encode()),
        mimetype="application/octet-stream",  # stops browsers adding .txt
        as_attachment=True,
        download_name=f"{safe_filename(title)}.magnet",
        max_age=0,
    )


@app.route("/send", methods=["POST"])
def send():
    if not client:
        return jsonify({"message": "No download client configured"}), 400
    data = request.json or {}
    link, title = data.get("link", ""), data.get("title", "")
    if not title or not _valid_details_link(link):
        return jsonify({"message": "Invalid request"}), 400
    try:
        client.add(extract_magnet_link(link), f"{SAVE_PATH_BASE}/{safe_filename(title)}")
        return jsonify({"message": "Sent to your download client"})
    except RateLimited as e:
        return jsonify({"message": str(e)}), 429
    except Exception as e:
        return jsonify({"message": f"Couldn't send: {e}"}), 500


@app.route("/status")
def status():
    if not client:
        abort(404)
    try:
        return render_template("status.html", torrents=client.list(), error=None)
    except Exception as e:
        return render_template("status.html", torrents=[],
                               error=f"Couldn't reach the download client: {e}")


# --- TorBox ------------------------------------------------------------------


def _tb_target(data):
    """Find the book a TorBox request is about: by infohash (TorBox page) or link (results).

    Returns (hash, magnet, link, title).
    """
    if not torbox.ENABLED:
        abort(404)
    h = (data.get("hash") or "").strip().lower()
    if h:
        rec = abb.store.get("torbox_watch", h)
        if not rec:
            raise ValueError("That book isn't on the TorBox list.")
        return h, rec["magnet"], rec.get("link"), rec.get("title")
    link = data.get("link", "")
    if not _valid_details_link(link):
        raise ValueError("Invalid link")
    magnet = extract_magnet_link(link)
    return torbox.infohash(magnet), magnet, link, data.get("title") or None


def _tb_error(e):
    if isinstance(e, RateLimited):
        return jsonify({"message": str(e)}), 429
    if isinstance(e, (torbox.TorBoxError, ValueError)):
        return jsonify({"message": str(e)}), 400
    print(f"[ERROR] TorBox: {e}")
    return jsonify({"message": f"TorBox request failed: {e}"}), 502


def _tb_json(fn):
    try:
        return jsonify(fn(request.json or {}))
    except Exception as e:
        return _tb_error(e)


@app.route("/torbox")
def torbox_page():
    if not torbox.ENABLED:
        abort(404)
    books = [torbox._public(r) for r in torbox.watch_list()]
    order = {"downloading": 0, "queued": 1, "error": 2, "ready": 3, "gave_up": 4, "removed": 5}
    books.sort(key=lambda b: (order.get(b["state"], 9), b["waiting_seconds"]))
    return render_template("torbox.html", books=books, settings={
        "poll": torbox.POLL_MINUTES, "reannounce": torbox.REANNOUNCE_HOURS,
        "give_up": torbox.GIVE_UP_DAYS, "remove": torbox.REMOVE_ON_GIVE_UP,
    })


@app.route("/torbox/peek", methods=["POST"])
def torbox_peek():
    """Status for results whose magnet we already know - never contacts AudioBook Bay."""
    if not torbox.ENABLED:
        abort(404)
    links = [l for l in (request.json or {}).get("links", [])[:200] if _valid_details_link(l)]
    by_hash = {}
    for link in links:
        magnet = abb.store.get("magnet", link)
        if magnet:
            try:
                by_hash.setdefault(torbox.infohash(magnet), []).append(link)
            except torbox.TorBoxError:
                pass
    if not by_hash:
        return jsonify({})
    try:
        states = torbox.statuses(list(by_hash))
    except Exception as e:
        return _tb_error(e)
    return jsonify({link: states[h] for h, ls in by_hash.items() for link in ls})


@app.route("/torbox/status", methods=["POST"])
def torbox_status():
    return _tb_json(lambda d: torbox.status(_tb_target(d)[0]))


@app.route("/torbox/add", methods=["POST"])
def torbox_add():
    def go(d):
        h, magnet, link, title = _tb_target(d)
        return torbox.add(magnet, name=title, link=link)
    return _tb_json(go)


@app.route("/torbox/retry", methods=["POST"])
def torbox_retry():
    return _tb_json(lambda d: torbox.retry(_tb_target(d)[0]))


@app.route("/torbox/remove", methods=["POST"])
def torbox_remove():
    def go(d):
        torbox.remove(_tb_target(d)[0])
        return {"state": "removed_by_you"}
    return _tb_json(go)


@app.route("/torbox/check-now", methods=["POST"])
def torbox_check_now():
    """Run the background check immediately (the TorBox page's Refresh button)."""
    if not torbox.ENABLED:
        abort(404)
    return _tb_json(lambda d: torbox.poll_once())


@app.route("/torbox/download")
def torbox_download():
    """Redirect to a fresh TorBox download link (zip of the book, or one file)."""
    file_id = request.args.get("file")
    try:
        h, magnet, link, title = _tb_target(request.args)
        url = torbox.download_url(magnet, file_id=int(file_id) if file_id else None, link=link, name=title)
    except RateLimited as e:
        return render_template("error.html", message=str(e)), 429
    except (torbox.TorBoxError, ValueError) as e:
        return render_template("error.html", message=str(e)), 400
    except Exception as e:
        print(f"[ERROR] TorBox download: {e}")
        return render_template("error.html", message=f"TorBox request failed: {e}"), 502
    return redirect(url)


torbox.start_worker()

if __name__ == "__main__":
    # waitress is a production web server; Flask's built-in one isn't meant
    # for anything reachable from the internet.
    from waitress import serve

    print(f"Serving on port {FLASK_PORT}", flush=True)
    serve(app, host="0.0.0.0", port=FLASK_PORT, threads=8)
