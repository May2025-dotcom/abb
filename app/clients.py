"""
Thin wrapper around the supported torrent clients.

Every client exposes the same two calls:
    add(magnet, save_path)  -> sends a magnet link to the client
    list()                  -> returns torrents in our category as plain dicts

If DOWNLOAD_CLIENT is not set, get_client() returns None and the app runs in
"direct" mode: magnet links are handed to the browser instead of a server.
"""

from qbittorrentapi import Client as QBClient
from transmission_rpc import Client as TransmissionClient
from deluge_web_client import DelugeWebClient, TorrentOptions

SUPPORTED = ("qbittorrent", "transmission", "delugeweb")


def _mb(size_bytes):
    return f"{size_bytes / (1024 * 1024):.2f} MB"


class QBittorrent:
    def __init__(self, cfg):
        self.cfg = cfg

    def _connect(self):
        qb = QBClient(
            host=self.cfg["host"],
            port=self.cfg["port"],
            username=self.cfg["username"],
            password=self.cfg["password"],
        )
        qb.auth_log_in()
        return qb

    def add(self, magnet, save_path):
        self._connect().torrents_add(
            urls=magnet, save_path=save_path, category=self.cfg["category"]
        )

    def list(self):
        return [
            {
                "name": t.name,
                "progress": round(t.progress * 100, 1),
                "state": t.state,
                "size": _mb(t.total_size),
            }
            for t in self._connect().torrents_info(category=self.cfg["category"])
        ]


class Transmission:
    def __init__(self, cfg):
        self.cfg = cfg

    def _connect(self):
        return TransmissionClient(
            host=self.cfg["host"],
            port=self.cfg["port"],
            protocol=self.cfg["scheme"],
            username=self.cfg["username"],
            password=self.cfg["password"],
        )

    def add(self, magnet, save_path):
        self._connect().add_torrent(magnet, download_dir=save_path)

    def list(self):
        # Transmission has no categories, so match on our save path instead.
        base = (self.cfg["save_path_base"] or "").rstrip("/")
        torrents = self._connect().get_torrents()
        if base:
            torrents = [t for t in torrents if (t.download_dir or "").startswith(base)]
        return [
            {
                "name": t.name,
                "progress": round(t.progress, 1),
                "state": str(t.status),
                "size": _mb(t.total_size),
            }
            for t in torrents
        ]


class Deluge:
    def __init__(self, cfg):
        self.cfg = cfg

    def _connect(self):
        client = DelugeWebClient(url=self.cfg["url"], password=self.cfg["password"])
        client.login()
        return client

    def add(self, magnet, save_path):
        options = TorrentOptions(download_location=save_path, label=self.cfg["category"])
        self._connect().add_torrent_magnet(magnet, torrent_options=options)

    def list(self):
        result = self._connect().get_torrents_status(
            filter_dict={"label": self.cfg["category"]},
            keys=["name", "state", "progress", "total_size"],
        )
        return [
            {
                "name": t["name"],
                "progress": round(t["progress"], 1),
                "state": t["state"],
                "size": _mb(t["total_size"]),
            }
            for t in result.result.values()
        ]


def get_client(name, cfg):
    name = (name or "").strip().lower()
    if not name or name == "none":
        return None
    if name == "qbittorrent":
        return QBittorrent(cfg)
    if name == "transmission":
        return Transmission(cfg)
    if name == "delugeweb":
        return Deluge(cfg)
    raise ValueError(
        f"Unsupported DOWNLOAD_CLIENT '{name}'. Use one of {', '.join(SUPPORTED)}, or leave it unset."
    )
