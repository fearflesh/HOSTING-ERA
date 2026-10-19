"""
Durable storage for the hosting panel.

Render's free disk is wiped on every restart / crash / redeploy.  This module
mirrors everything that matters into a MongoDB database (Atlas free tier is
enough) and restores it on boot:

  * panel database   -> one document per user / bot / coupon / ticket ...
  * panel settings   -> one document per setting
  * uploaded bot files (already Fernet-encrypted) and their key cache
  * admin banners/photos
  (Data that hosted bots create while running is deliberately NOT stored; only
   the uploaded source / zip files and their requirements are.)

Everything that holds user data is encrypted before it leaves the server with
a key derived from PERSIST_SECRET, so the database alone is useless to anyone
who gets hold of it.

Only ONE panel instance may write at a time.  On boot an instance claims a
"lease"; an older instance that is still alive (Render deploys overlap for a
few seconds) notices it lost the lease and stops writing.

The module is a no-op when MONGO_URL is not set, so the panel still runs
(with the old GitHub backup) without a database.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

SKIP_DIRS = {"node_modules", "venv", ".venv", "site-packages", "__pycache__", ".git",
             ".cache", ".npm", ".pip"}
CODE_EXT = {".py", ".js", ".ts", ".mjs", ".cjs"}
BLOB_PREFIXES = ("enc/", "key/", "photo/")


def _dumps(o: Any) -> str:
    return json.dumps(o, ensure_ascii=False, sort_keys=True, default=str)


def _h(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8", "ignore")).hexdigest()


# ════════════════════════════════════════════════════════════════════════
#  Backends
# ════════════════════════════════════════════════════════════════════════
class MongoBackend:
    def __init__(self, url: str, dbname: str) -> None:
        from pymongo import MongoClient, ReplaceOne  # type: ignore
        import gridfs  # type: ignore
        self._ReplaceOne = ReplaceOne
        self.client = MongoClient(url, serverSelectionTimeoutMS=8000, connectTimeoutMS=8000,
                                  socketTimeoutMS=60000, appname="fsh-panel")
        self.client.admin.command("ping")
        self.db = self.client[dbname]
        self.fs = gridfs.GridFSBucket(self.db, bucket_name="blobs")
        self.files = self.db["blobs.files"]

    def colls(self) -> List[str]:
        return [n for n in self.db.list_collection_names()
                if n.startswith("t_") or n in ("misc", "settings")]

    def docs_list(self, coll: str) -> Dict[str, str]:
        return {d["_id"]: d["j"] for d in self.db[coll].find({}, {"j": 1})}

    def docs_write(self, coll: str, ups: Dict[str, str], dels: List[str]) -> None:
        ops = [self._ReplaceOne({"_id": i}, {"j": j}, upsert=True) for i, j in ups.items()]
        if ops:
            self.db[coll].bulk_write(ops, ordered=False)
        if dels:
            self.db[coll].delete_many({"_id": {"$in": list(dels)}})

    def docs_count(self, coll: str) -> int:
        return int(self.db[coll].estimated_document_count())

    def kv_get(self, key: str) -> Any:
        d = self.db["kv"].find_one({"_id": key})
        return d.get("v") if d else None

    def kv_set(self, key: str, val: Any) -> None:
        self.db["kv"].replace_one({"_id": key}, {"v": val}, upsert=True)

    def blob_put(self, name: str, data: bytes, meta: Dict[str, Any]) -> None:
        old = [f["_id"] for f in self.files.find({"filename": name}, {"_id": 1})]
        self.fs.upload_from_stream(name, data, metadata=meta)     # new copy first, then drop old
        for oid in old:
            try:
                self.fs.delete(oid)
            except Exception:
                pass

    def blob_get(self, name: str) -> Optional[Tuple[bytes, Dict[str, Any]]]:
        f = self.files.find_one({"filename": name}, sort=[("uploadDate", -1)])
        if not f:
            return None
        return self.fs.open_download_stream(f["_id"]).read(), (f.get("metadata") or {})

    def blob_list(self) -> Dict[str, Dict[str, Any]]:
        out: Dict[str, Dict[str, Any]] = {}
        for f in self.files.find({}, {"filename": 1, "length": 1, "metadata": 1}):
            out[f["filename"]] = {"size": f.get("length", 0), "meta": f.get("metadata") or {}}
        return out

    def blob_delete(self, name: str) -> None:
        for f in list(self.files.find({"filename": name}, {"_id": 1})):
            try:
                self.fs.delete(f["_id"])
            except Exception:
                pass

    def storage_bytes(self) -> int:
        try:
            return int(self.db.command("dbStats").get("dataSize", 0))
        except Exception:
            return 0


class MemoryBackend:
    """In-memory stand-in (tests / dry runs). Same API as MongoBackend."""

    def __init__(self) -> None:
        self.d: Dict[str, Dict[str, str]] = {}
        self.kv: Dict[str, Any] = {}
        self.blobs: Dict[str, Tuple[bytes, Dict[str, Any]]] = {}

    def colls(self) -> List[str]:
        return list(self.d)

    def docs_list(self, coll: str) -> Dict[str, str]:
        return dict(self.d.get(coll, {}))

    def docs_write(self, coll: str, ups: Dict[str, str], dels: List[str]) -> None:
        c = self.d.setdefault(coll, {})
        c.update(ups)
        for i in dels:
            c.pop(i, None)

    def docs_count(self, coll: str) -> int:
        return len(self.d.get(coll, {}))

    def kv_get(self, key: str) -> Any:
        return self.kv.get(key)

    def kv_set(self, key: str, val: Any) -> None:
        self.kv[key] = val

    def blob_put(self, name: str, data: bytes, meta: Dict[str, Any]) -> None:
        self.blobs[name] = (data, meta)

    def blob_get(self, name: str) -> Optional[Tuple[bytes, Dict[str, Any]]]:
        return self.blobs.get(name)

    def blob_list(self) -> Dict[str, Dict[str, Any]]:
        return {n: {"size": len(v[0]), "meta": v[1]} for n, v in self.blobs.items()}

    def blob_delete(self, name: str) -> None:
        self.blobs.pop(name, None)

    def storage_bytes(self) -> int:
        return sum(len(j) for c in self.d.values() for j in c.values()) + \
               sum(len(v[0]) for v in self.blobs.values())


# ════════════════════════════════════════════════════════════════════════
#  Persist
# ════════════════════════════════════════════════════════════════════════
class Persist:
    def __init__(self, *, url: str, dbname: str, db_file: Path, settings_file: Path,
                 dirs: Dict[str, Path], secret_fn: Callable[[], str],
                 log: Callable[..., None] = print,
                 backend: Any = None,
                 on_files_written: Optional[Callable[[], None]] = None) -> None:
        self.url = (url or "").strip()
        self.dbname = dbname or "fsh_panel"
        self.db_file, self.settings_file = db_file, settings_file
        self.dirs = dirs
        self.secret_fn = secret_fn
        self.log = log
        self.b: Any = backend
        self.on_files_written = on_files_written
        self.me = uuid.uuid4().hex[:12]
        self.restored = False          # True when this boot restored state from the database
        self.degraded = False          # DB configured but unreachable at boot
        self.fenced = False            # another instance took over the lease
        self.last_flush = 0.0
        self.last_error = ""
        self.flush_every = float(os.environ.get("PERSIST_FLUSH_SEC", "2") or 2)
        self.encrypt = os.environ.get("PERSIST_ENCRYPT", "1") != "0"
        self._fernet: Any = None
        self._io = threading.RLock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._known: Dict[str, Dict[str, str]] = {}      # coll -> {doc id: hash}
        self._file_sig: Dict[str, Tuple[int, int]] = {}
        self._blob_sig: Dict[str, Tuple[int, int]] = {}
        self._last_lease_beat = 0.0

    # ── helpers ───────────────────────────────────────────────────────
    @property
    def enabled(self) -> bool:
        return self.b is not None and not self.degraded

    def _f(self) -> Any:
        if self._fernet is None:
            from cryptography.fernet import Fernet
            raw = hashlib.sha256(("persist|" + (self.secret_fn() or "")).encode()).digest()
            self._fernet = Fernet(base64.urlsafe_b64encode(raw))
        return self._fernet

    def _enc(self, s: str) -> str:
        return "enc:" + self._f().encrypt(s.encode()).decode() if self.encrypt else s

    def _dec(self, s: str) -> str:
        if s.startswith("enc:"):
            return self._f().decrypt(s[4:].encode()).decode()
        return s

    # ── connect / lease ───────────────────────────────────────────────
    def connect(self, retries: int = 4, delay: float = 4.0) -> bool:
        if self.b is not None:
            return True
        if not self.url:
            return False
        for i in range(retries):
            try:
                self.b = MongoBackend(self.url, self.dbname)
                return True
            except Exception as e:
                self.last_error = f"connect: {e}"
                self.log(f"[persist] cannot reach database ({e}); retry {i + 1}/{retries}")
                time.sleep(delay)
        self.b = None
        return False

    def _claim_lease(self) -> None:
        cur = self.b.kv_get("lease")
        if cur and cur.get("holder") != self.me and time.time() - float(cur.get("ts", 0)) < 90:
            self.log("[persist] another instance is still writing; waiting for it to stop")
            time.sleep(self.flush_every * 2 + 2)
        self.b.kv_set("lease", {"holder": self.me, "ts": time.time()})
        self._last_lease_beat = time.time()

    def _lease_ok(self) -> bool:
        if self.fenced:
            return False
        cur = self.b.kv_get("lease")
        if cur and cur.get("holder") != self.me:
            self.fenced = True
            self.log("[persist] lost the lease to a newer instance — this one stops writing")
            return False
        if time.time() - self._last_lease_beat > 30:
            self.b.kv_set("lease", {"holder": self.me, "ts": time.time()})
            self._last_lease_beat = time.time()
        return True

    # ── db / settings  <->  documents ─────────────────────────────────
    @staticmethod
    def _split(kind: str, data: Dict[str, Any]) -> Dict[str, Dict[str, str]]:
        if kind == "settings":
            return {"settings": {str(k): _dumps(v) for k, v in data.items()}}
        groups: Dict[str, Dict[str, str]] = {}
        misc: Dict[str, str] = {}
        for k, v in data.items():
            if isinstance(v, dict) and v and all(isinstance(x, dict) for x in v.values()):
                groups["t_" + str(k)] = {str(i): _dumps(x) for i, x in v.items()}
            else:
                misc[str(k)] = _dumps(v)
        if misc:
            groups["misc"] = misc
        return groups

    def _push_dict(self, kind: str, data: Dict[str, Any]) -> int:
        groups = self._split(kind, data)
        colls = {c for c in self._known if (c == "settings") == (kind == "settings")} | set(groups)
        n = 0
        for coll in colls:
            cur = groups.get(coll, {})
            prev = self._known.get(coll, {})
            ups = {i: j for i, j in cur.items() if prev.get(i) != _h(j)}
            dels = [i for i in prev if i not in cur]
            if not ups and not dels:
                continue
            self.b.docs_write(coll, {i: self._enc(j) for i, j in ups.items()}, dels)
            kn = dict(prev)
            for i, j in ups.items():
                kn[i] = _h(j)
            for i in dels:
                kn.pop(i, None)
            self._known[coll] = kn
            n += len(ups) + len(dels)
        return n

    def flush(self, force: bool = False) -> int:
        """Push database/settings changes. Cheap when nothing changed."""
        if not self.enabled:
            return 0
        with self._io:
            n = 0
            for path, kind in ((self.db_file, "db"), (self.settings_file, "settings")):
                try:
                    st = path.stat()
                except FileNotFoundError:
                    continue
                sig = (st.st_mtime_ns, st.st_size)
                if not force and sig == self._file_sig.get(kind):
                    continue
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                except Exception:
                    continue
                if not isinstance(data, dict):
                    continue
                if not self._lease_ok():
                    return n
                n += self._push_dict(kind, data)
                self._file_sig[kind] = sig
            if n:
                self.last_flush = time.time()
            return n

    def _rebase(self, db: Dict[str, Any]) -> None:
        """Re-point stored absolute paths at this server's data directory."""
        for b in (db.get("bots") or {}).values():
            if not isinstance(b, dict):
                continue
            if b.get("dir"):
                b["dir"] = str(self.dirs["sandbox"] / Path(str(b["dir"])).name)
            for f in b.get("enc_files") or []:
                p = f.get("enc_path")
                if p:
                    parts = Path(str(p)).parts[-2:]
                    f["enc_path"] = str(self.dirs["encfiles"].joinpath(*parts))

    def pull_db(self) -> bool:
        """Rebuild the local database + settings files from the database.
        Returns False when the database holds nothing yet."""
        names = set(self.b.colls())
        db: Dict[str, Any] = {}
        settings: Dict[str, Any] = {}
        misc: Dict[str, Any] = {}
        for c in sorted(names):
            rows = self.b.docs_list(c)
            if not rows:
                continue
            decoded = {i: json.loads(self._dec(j)) for i, j in rows.items()}
            if c == "settings":
                settings = decoded
            elif c == "misc":
                misc = decoded
            elif c.startswith("t_"):
                db[c[2:]] = decoded
        for k, v in misc.items():
            db.setdefault(k, v)
        if not db and not settings:
            return False
        self._rebase(db)
        with self._io:
            for path, payload in ((self.db_file, db), (self.settings_file, settings)):
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix(path.suffix + ".tmp")
                tmp.write_text(json.dumps(payload, indent=2, default=str, ensure_ascii=False),
                               encoding="utf-8")
                tmp.replace(path)
            self._known = {}
            for kind, payload in (("db", db), ("settings", settings)):
                for coll, rows in self._split(kind, payload).items():
                    self._known[coll] = {i: _h(j) for i, j in rows.items()}
            for path, kind in ((self.db_file, "db"), (self.settings_file, "settings")):
                st = path.stat()
                self._file_sig[kind] = (st.st_mtime_ns, st.st_size)
        if self.on_files_written:
            self.on_files_written()
        return True

    def push_all(self) -> int:
        """Overwrite the database with the local files (first run / manual)."""
        with self._io:
            if not self._lease_ok():
                return 0
            self._known = {}
            self._file_sig = {}
            n = self.flush(force=True)
            self.sync_blobs(force=True)
            return n

    # ── blobs (encrypted uploads, key cache, photos) ──────────────────
    def _blob_map(self) -> Dict[str, Path]:
        m: Dict[str, Path] = {}
        enc = self.dirs["encfiles"]
        if enc.exists():
            for p in enc.rglob("*.enc"):
                m["enc/" + p.relative_to(enc).as_posix()] = p
        kc = self.dirs["data"] / "keycache"
        if kc.exists():
            for p in kc.glob("*.bin"):
                m["key/" + p.name] = p
        ph = self.dirs.get("photos")
        if ph and ph.exists():
            for p in ph.glob("*"):
                try:
                    if p.is_file() and p.stat().st_size <= 3 * 1024 * 1024:
                        m["photo/" + p.name] = p
                except OSError:
                    pass
        return m

    def _local_for(self, name: str) -> Optional[Path]:
        if name.startswith("enc/"):
            return self.dirs["encfiles"].joinpath(*name[4:].split("/"))
        if name.startswith("key/"):
            return self.dirs["data"] / "keycache" / name[4:]
        if name.startswith("photo/") and self.dirs.get("photos"):
            return self.dirs["photos"] / name[6:]
        return None

    def sync_blobs(self, force: bool = False) -> int:
        if not self.enabled:
            return 0
        with self._io:
            cur = self._blob_map()
            n = 0
            for name, p in cur.items():
                try:
                    st = p.stat()
                except OSError:
                    continue
                sig = (st.st_size, st.st_mtime_ns)
                if not force and self._blob_sig.get(name) == sig:
                    continue
                if not self._lease_ok():
                    return n
                self.b.blob_put(name, p.read_bytes(), {"t": int(time.time())})
                self._blob_sig[name] = sig
                n += 1
            gone = [k for k in self._blob_sig if k.startswith(BLOB_PREFIXES) and k not in cur]
            # safety valve: if most tracked files vanished at once, assume the disk was
            # wiped (not deliberate deletes) and keep the copies in the database.
            if gone and (len(gone) <= 5 or len(gone) * 2 < len(self._blob_sig)):
                for k in gone:
                    if not self._lease_ok():
                        break
                    self.b.blob_delete(k)
                    self._blob_sig.pop(k, None)
            elif gone:
                self.log(f"[persist] {len(gone)} tracked files vanished at once — not deleting remote copies")
            return n

    def _purge_runtime_data(self) -> None:
        """Remove bot runtime-data snapshots left by earlier versions."""
        try:
            for name in list(self.b.blob_list()):
                if name.startswith("botdata/"):
                    self.b.blob_delete(name)
        except Exception as e:
            self.log(f"[persist] purge failed: {e}")

    def restore_blobs(self) -> int:
        n = 0
        for name in self.b.blob_list():
            p = self._local_for(name)
            if p is None:
                continue
            if p.exists():
                st = p.stat()
                got = self.b.blob_get(name)
                if got and len(got[0]) == st.st_size:
                    self._blob_sig[name] = (st.st_size, st.st_mtime_ns)
                continue
            got = self.b.blob_get(name)
            if not got:
                continue
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(got[0])
            st = p.stat()
            self._blob_sig[name] = (st.st_size, st.st_mtime_ns)
            n += 1
        return n

    # ── lifecycle ─────────────────────────────────────────────────────
    def boot(self) -> Dict[str, Any]:
        """Connect, claim the lease, restore everything. Never raises."""
        info: Dict[str, Any] = {"enabled": False, "restored": False, "migrated": False,
                                "degraded": False, "blobs": 0}
        if not self.url and self.b is None:
            return info
        if not self.connect():
            self.degraded = True
            info["degraded"] = True
            return info
        info["enabled"] = True
        try:
            self._claim_lease()
            if self.pull_db():
                self.restored = True
                info["restored"] = True
            else:
                info["migrated"] = bool(self.push_all())
            info["blobs"] = self.restore_blobs()
            self._purge_runtime_data()
            if info["migrated"]:
                self.sync_blobs(force=True)
        except Exception as e:
            # a wrong PERSIST_SECRET lands here — never overwrite the database in that case
            self.last_error = f"boot: {e}"
            self.log(f"[persist] boot failed: {e!r} — running in degraded mode (nothing will be written)")
            self.degraded = True
            info["degraded"] = True
            info["error"] = repr(e)
            return info
        self.start()
        return info

    def recover(self, mode: str) -> str:
        """Owner command: leave degraded mode. mode = 'push' (local wins) or 'pull' (database wins)."""
        if not self.url and self.b is None:
            return "MONGO_URL is not set."
        if not self.connect(retries=2, delay=2):
            return "Database still unreachable."
        self.degraded = False
        self.fenced = False
        try:
            self._claim_lease()
            if mode == "pull":
                msg = "pulled from the database" if self.pull_db() else "database is empty — nothing pulled"
                self.restore_blobs()
            else:
                self.push_all()
                msg = "pushed local state to the database"
        except Exception as e:
            self.degraded = True
            self.last_error = f"recover: {e}"
            return f"Failed: {e!r}"
        self.start()
        return msg

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="persist-flush")
        self._thread.start()

    def _loop(self) -> None:
        last_blob = 0.0
        while not self._stop.is_set():
            try:
                if self.enabled and not self.fenced:
                    self.flush()
                    if time.time() - last_blob >= 10:
                        self.sync_blobs()
                        last_blob = time.time()
            except Exception as e:
                self.last_error = str(e)
                self.log(f"[persist] flush error: {e}")
                self._stop.wait(5)
            self._stop.wait(self.flush_every)

    def shutdown(self, budget_sec: float = 20.0) -> None:
        """Final flush for SIGTERM (Render sends it before every restart)."""
        if not self.enabled:
            return
        t0 = time.time()
        try:
            self.flush()
            self.sync_blobs()
        except Exception as e:
            self.log(f"[persist] shutdown flush error: {e}")

    def status(self) -> Dict[str, Any]:
        st: Dict[str, Any] = {
            "configured": bool(self.url) or self.b is not None,
            "connected": self.b is not None and not self.degraded,
            "degraded": self.degraded, "fenced": self.fenced, "restored_on_boot": self.restored,
            "last_flush_ago": int(time.time() - self.last_flush) if self.last_flush else None,
            "last_error": self.last_error, "instance": self.me, "encrypted": self.encrypt,
        }
        if self.b is not None and not self.degraded:
            try:
                st["docs"] = {c: self.b.docs_count(c) for c in sorted(self.b.colls())}
                bl = self.b.blob_list()
                st["blobs"] = len(bl)
                st["blob_mb"] = round(sum(v["size"] for v in bl.values()) / 1048576, 2)
                st["storage_mb"] = round(self.b.storage_bytes() / 1048576, 2)
            except Exception as e:
                st["status_error"] = str(e)
        return st
