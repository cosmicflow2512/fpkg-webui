#!/usr/bin/env python3
"""FPKG WebUI - web front end for PSVIETHOA fpkg-cli (PS5 FPKG builder) on Unraid.

Python stdlib only. One build at a time, further jobs wait in a queue.
"""
import glob
import hashlib
import io
import json
import logging
import logging.handlers
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import traceback
import uuid
import urllib.parse
import urllib.request
import zipfile
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

from exfat import ExfatError, ExfatImage

APP_VERSION = "1.2.5"
ENV = os.environ.get
PORT = int(ENV("PORT", "8095"))
DATA = ENV("DATA_DIR", "/config")
CLI = ENV("FPKG_CLI", "fpkg-cli")
SEVENZ = ENV("SEVENZ", "7z")
ROOTS_SPEC = ENV("BROWSE_ROOTS", "/shares/*:/output:/work")
DEF_OUT = ENV("DEFAULT_OUT", "/output")
DEF_WORK = ENV("DEFAULT_WORK", "/work")
PUID, PGID = int(ENV("PUID", "99")), int(ENV("PGID", "100"))
HERE = os.path.dirname(os.path.abspath(__file__))
STARTED = time.time()

ARCHIVES = (".7z", ".zip", ".rar", ".tar", ".tgz", ".gz", ".xz", ".bz2", ".001")
IMAGES = (".exfat", ".ffpfsc", ".ffpkg")
MAX_NEST = 3  # inner archive levels unpacked automatically
JUNK = {".DS_Store", "Thumbs.db", "desktop.ini", "__MACOSX", ".fseventsd", ".Spotlight-V100", ".Trashes"}
COPY_CHUNK = 16 * 1024 * 1024

os.makedirs(os.path.join(DATA, "logs", "jobs"), exist_ok=True)
os.umask(0)

# ---------------------------------------------------------------- logging
LOG = logging.getLogger("fpkg-webui")
LOG.setLevel(logging.DEBUG if ENV("LOG_LEVEL", "INFO").upper() == "DEBUG" else logging.INFO)
_fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
_fh = logging.handlers.RotatingFileHandler(os.path.join(DATA, "logs", "server.log"), maxBytes=2_000_000,
                                           backupCount=3, encoding="utf-8")
_fh.setFormatter(_fmt)
_sh = logging.StreamHandler(sys.stdout)
_sh.setFormatter(_fmt)
LOG.addHandler(_fh)
LOG.addHandler(_sh)

JOBS = {}
ORDER = []
LOCK = threading.Lock()
QUEUE_CV = threading.Condition(LOCK)


class Cancelled(Exception):
    pass


class JobError(Exception):
    pass


# ---------------------------------------------------------------- roots / paths
def roots():
    out = []
    for part in ROOTS_SPEC.split(":"):
        part = part.strip()
        if not part:
            continue
        if part.endswith("/*"):
            base = part[:-2]
            if os.path.isdir(base):
                for n in sorted(os.listdir(base)):
                    p = os.path.join(base, n)
                    if os.path.isdir(p) and not n.startswith("."):
                        out.append(os.path.realpath(p))
        elif os.path.isdir(part):
            out.append(os.path.realpath(part))
    seen, res = set(), []
    for r in out:
        if r not in seen:
            seen.add(r)
            res.append(r)
    return res


def root_label(r):
    if r == os.path.realpath(DEF_OUT):
        return "Ausgabe"
    if r == os.path.realpath(DEF_WORK):
        return "Arbeitsordner"
    return os.path.basename(r) or r


def root_of(rp):
    for r in roots():
        if rp == r or rp.startswith(r + os.sep):
            return r
    return None


def safe_path(p, must_exist=True):
    if not p or not os.path.isabs(p):
        raise JobError(f"Pfad muss absolut sein: {p!r}")
    rp = os.path.realpath(p)
    if not root_of(rp):
        raise JobError(f"Pfad liegt außerhalb der freigegebenen Ordner ({', '.join(roots()) or 'keine'}): {p}")
    if must_exist and not os.path.exists(rp):
        raise JobError(f"Pfad existiert nicht: {p}")
    return rp


# ---------------------------------------------------------------- helpers
def human(n):
    if n is None:
        return "?"
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or u == "TB":
            return f"{n:.1f} {u}" if u != "B" else f"{int(n)} B"
        n /= 1024


def fmt_eta(sec):
    if sec is None or sec < 0 or sec > 10 * 86400:
        return ""
    sec = int(sec)
    h, m, s = sec // 3600, sec % 3600 // 60, sec % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def lower_ext(p):
    n = p.lower()
    if (re.search(r"\.\d{3}$", n) or re.search(r"\.part\d+\.rar$", n)
            or re.search(r"\.(r\d{2,3}|[s-y]\d{2}|z\d{2,3})$", n)):
        return ".001"
    for e in ARCHIVES + IMAGES + (".pkg",):
        if n.endswith(e):
            return e
    return os.path.splitext(n)[1]


def find_app_dir(base, depth=5):
    for cur, dirs, files in os.walk(base):
        dirs[:] = sorted(d for d in dirs if d not in JUNK)
        if "sce_sys" in dirs and "eboot.bin" in files:
            return cur
        if cur[len(base):].count(os.sep) >= depth:
            dirs[:] = []
    return None


def find_files(base, exts, depth=4):
    hits = []
    for cur, dirs, files in os.walk(base):
        dirs[:] = sorted(d for d in dirs if d not in JUNK)
        hits += [os.path.join(cur, f) for f in sorted(files)
                 if f.lower().endswith(exts) and not f.startswith("._")]
        if cur[len(base):].count(os.sep) >= depth:
            dirs[:] = []
    return hits


def first_volume(paths):
    """Prefer .part1.rar / .001 among multi-volume candidates."""
    for p in paths:
        n = os.path.basename(p).lower()
        if re.search(r"\.part0*1\.rar$", n) or n.endswith(".001"):
            return p
    for p in paths:
        n = os.path.basename(p).lower()
        if n.endswith(ARCHIVES) and not re.search(r"\.part\d+\.rar$", n) and not re.search(r"\.\d{3}$", n):
            return p
    return paths[0]


def classify(path):
    """-> (kind, path) with kind in folder|archive|exfat|ffpfsc|ffpkg|pkg."""
    if os.path.isdir(path):
        app = find_app_dir(path)
        if app:
            return "folder", app
        imgs = find_files(path, IMAGES)
        if imgs:
            return classify(imgs[0])
        pkgs = find_files(path, (".pkg",))
        if pkgs:
            return "pkg", pkgs[0]
        arcs = find_files(path, ARCHIVES, depth=1)
        if arcs:
            return "archive", first_volume(arcs)
        raise JobError(f"Kein App-Ordner (sce_sys + eboot.bin), kein Image, kein .pkg und kein Archiv in {path}")
    ext = lower_ext(path)
    if ext in ARCHIVES:
        return "archive", first_volume(archive_volumes(path))
    if ext in IMAGES:
        return ext[1:], path
    if ext == ".pkg":
        return "pkg", path
    raise JobError(f"Unbekannter Dateityp: {path}")


def archive_volumes(path):
    """All files belonging to a (multi-volume) archive."""
    d, name = os.path.split(path)
    low = name.lower()
    pats = []
    # split files: name.7z.001 …, but also plain splits like game.exfat.001 / name.001 (7-Zip "Split" format)
    m = re.match(r"^(.*)\.\d{3}$", low)
    if m:
        pats.append(re.escape(m.group(1)) + r"\.\d{3}")
    # old RAR naming continues .r00-.r99, .s00-.s99, .t00 … (big sets like DUPLEX inner archives)
    m = re.match(r"^(.*)\.(r(?=\d{2,3}$)|[s-y](?=\d{2}$)|z(?=\d{2,3}$))\d+$", low)
    if m:
        low = m.group(1) + (".zip" if m.group(2) == "z" else ".rar")
    m = re.match(r"^(.*)\.part\d+\.rar$", low)
    if m:
        pats.append(re.escape(m.group(1)) + r"\.part\d+\.rar")
    elif low.endswith(".rar"):
        b = re.escape(low[:-4])
        pats += [b + r"\.rar", b + r"\.r\d{2,3}", b + r"\.[s-y]\d{2}"]
    elif low.endswith(".zip"):
        b = re.escape(low[:-4])
        pats += [b + r"\.zip", b + r"\.z\d{2}"]
    if not pats:
        return [path]
    rx = re.compile("^(?:" + "|".join(pats) + ")$")
    vols = sorted(os.path.join(d, f) for f in os.listdir(d) if rx.match(f.lower()))
    return vols or [path]


def fix_root(base):
    hits = []
    for cur, dirs, files in os.walk(base):
        dirs[:] = sorted(d for d in dirs if d not in JUNK)
        if "sce_sys" in dirs or "eboot.bin" in files:
            hits.append(cur)
        if cur[len(base):].count(os.sep) >= 4:
            dirs[:] = []
    if hits:
        return min(hits, key=len)
    entries = [e for e in os.listdir(base) if e not in JUNK and not e.startswith("._")]
    if len(entries) == 1 and os.path.isdir(os.path.join(base, entries[0])):
        return fix_root(os.path.join(base, entries[0]))
    return base


def rel_files(base):
    out = []
    for cur, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if d not in JUNK]
        for f in files:
            if f in JUNK or f.startswith("._"):
                continue
            out.append(os.path.relpath(os.path.join(cur, f), base).replace(os.sep, "/"))
    return sorted(out)


def strip_junk(base):
    for cur, dirs, files in os.walk(base, topdown=True):
        for d in list(dirs):
            if d in JUNK:
                shutil.rmtree(os.path.join(cur, d), ignore_errors=True)
                dirs.remove(d)
        for f in files:
            if f in JUNK or f.startswith("._"):
                try:
                    os.remove(os.path.join(cur, f))
                except OSError:
                    pass


def dir_size(p):
    if os.path.isfile(p):
        return os.path.getsize(p)
    total = 0
    for cur, _, files in os.walk(p):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(cur, f))
            except OSError:
                pass
    return total


def disk(p):
    while p and not os.path.exists(p):
        p = os.path.dirname(p)
    try:
        u = shutil.disk_usage(p)
        return {"free": u.free, "total": u.total}
    except OSError:
        return {"free": None, "total": None}


def chown_tree(p):
    try:
        os.chown(p, PUID, PGID)
        for cur, dirs, files in os.walk(p):
            for n in dirs + files:
                os.lchown(os.path.join(cur, n), PUID, PGID)
    except OSError:
        pass


# ---------------------------------------------------------------- settings
SETTINGS_FILE = os.path.join(DATA, "settings.json")
DEFAULT_SETTINGS = {
    "overlap": True,
    "checksum": True,
    "watch_enabled": False,
    "watch_dir": "/shares/NZB/fpkg-inbox",
    "watch_fix_dir": "/shares/NZB/fpkg-fixes",
    "watch_stable": 120,
    "watch_interval": 30,
    "watch_preset": "standard",
    "watch_confirm": False,
    "watch_cleanup": True,
    "watch_delete_archive": False,
    "watch_after": "move",
    "archive_passwords": "",
    "pushover_enabled": False,
    "pushover_user": "",
    "pushover_token": "",
    "notify_done": True,
    "notify_failed": True,
    "notify_waiting": True,
    "webui_url": "",
}
SETTINGS = dict(DEFAULT_SETTINGS)
SECRET_KEYS = ("pushover_user", "pushover_token")
# values set via container environment (Unraid template) win over the UI and are shown read-only
ENV_SETTINGS = {"pushover_user": "PUSHOVER_USER", "pushover_token": "PUSHOVER_TOKEN", "webui_url": "WEBUI_URL"}
LOCKED = set()


def load_settings():
    stored = {}
    try:
        if os.path.exists(SETTINGS_FILE):
            stored = {k: v for k, v in json.load(open(SETTINGS_FILE)).items() if k in DEFAULT_SETTINGS}
            SETTINGS.update(stored)
    except Exception:
        LOG.exception("could not read settings.json")
    for key, env in ENV_SETTINGS.items():
        val = (ENV(env) or "").strip()
        if val:
            SETTINGS[key] = val
            LOCKED.add(key)
    if {"pushover_user", "pushover_token"} <= LOCKED and "pushover_enabled" not in stored:
        SETTINGS["pushover_enabled"] = True
    if LOCKED:
        LOG.info("settings from environment: %s", ", ".join(sorted(ENV_SETTINGS[k] for k in LOCKED)))


def save_settings():
    tmp = SETTINGS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump({k: v for k, v in SETTINGS.items() if k not in LOCKED}, f, indent=2)
    os.replace(tmp, SETTINGS_FILE)
    try:
        os.chmod(SETTINGS_FILE, 0o600)
    except OSError:
        pass


def mask(v):
    return ("••••" + v[-4:]) if v else ""


def public_settings():
    d = dict(SETTINGS)
    for k in SECRET_KEYS:
        d[k] = mask(d[k])
    d["locked"] = sorted(LOCKED)
    return d


def update_settings(b):
    for k, default in DEFAULT_SETTINGS.items():
        if k not in b or k in LOCKED:
            continue
        v = b[k]
        if k in SECRET_KEYS:
            v = (v or "").strip()
            if v.startswith("••••"):
                continue
        elif isinstance(default, bool):
            v = bool(v)
        elif isinstance(default, int):
            v = max(5, int(v))
        else:
            v = str(v or "").strip()
        if k in ("watch_dir", "watch_fix_dir") and v:
            v = safe_path(v, must_exist=False)
        if k == "watch_preset" and v not in ("fast", "standard", "smallest"):
            v = "standard"
        if k == "watch_after" and v not in ("move", "keep"):
            v = "move"
        SETTINGS[k] = v
    save_settings()
    LOG.info("settings saved (watch=%s overlap=%s pushover=%s)", SETTINGS["watch_enabled"], SETTINGS["overlap"],
             SETTINGS["pushover_enabled"])


# ---------------------------------------------------------------- space accounting
def _dev(p):
    while p and not os.path.exists(p):
        p = os.path.dirname(p)
    try:
        return os.stat(p).st_dev
    except OSError:
        return None


def free_for(path, me=None):
    """Free bytes on path minus space other active jobs on the same filesystem still expect to use."""
    free = disk(path)["free"]
    if free is None:
        return None
    dev = _dev(path)
    held = sum(j.reserve or 0 for j in list(JOBS.values())
               if j is not me and j.status in ("running", "waiting", "ready") and _dev(j.work) == dev)
    return max(0, free - held)


# ---------------------------------------------------------------- checksums
RX_HEX = re.compile(r"^[0-9a-fA-F]+$")
ALGO_BY_LEN = {64: "sha256", 32: "md5", 8: "crc32"}


def find_checksums(src):
    """Checksum entries next to src: {lower(filename) or '*': (algo, hash, origin_file)}."""
    d = os.path.dirname(src)
    out = {}
    try:
        names = os.listdir(d)
    except OSError:
        return out
    for n in sorted(names):
        low = n.lower()
        if not (low.endswith((".sfv", ".sha256", ".sha256sum", ".md5", ".sha"))
                or low.startswith(("sha256sums", "sha-256", "sha256", "md5sums", "checksum"))):
            continue
        path = os.path.join(d, n)
        try:
            if os.path.getsize(path) > 1_000_000:
                continue
            text = open(path, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        lines = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.strip().startswith((";", "#"))]
        for ln in lines:
            m = re.match(r"^(SHA256|MD5)\s*\((.+)\)\s*=\s*([0-9a-fA-F]+)$", ln)        # BSD style
            if m:
                out[os.path.basename(m.group(2)).lower()] = (m.group(1).lower(), m.group(3), path)
                continue
            m = re.match(r"^([0-9a-fA-F]{32}|[0-9a-fA-F]{64})\s+\*?(.+)$", ln)        # sha256sum / md5sum
            if m:
                out[os.path.basename(m.group(2).strip()).lower()] = (ALGO_BY_LEN[len(m.group(1))], m.group(1), path)
                continue
            m = re.match(r"^(.+?)\s+([0-9a-fA-F]{8})$", ln)                           # sfv
            if m and low.endswith(".sfv"):
                out[os.path.basename(m.group(1).strip()).lower()] = ("crc32", m.group(2), path)
                continue
            tok = ln.split()[0] if ln.split() else ""
            if len(lines) == 1 and RX_HEX.match(tok) and len(tok) in (32, 64):          # bare hash
                out.setdefault("*", (ALGO_BY_LEN[len(tok)], tok, path))
    return out


# ---------------------------------------------------------------- notifications
PUSHOVER_URL = "https://api.pushover.net/1/messages.json"


def send_pushover(title, message, priority=0, url=None):
    if not (SETTINGS["pushover_user"] and SETTINGS["pushover_token"]):
        return False, "User-Key oder API-Token fehlt"
    data = {"token": SETTINGS["pushover_token"], "user": SETTINGS["pushover_user"],
            "title": title[:250], "message": message[:1024], "priority": str(priority)}
    if url:
        data["url"], data["url_title"] = url[:512], "FPKG Builder öffnen"
    req = urllib.request.Request(PUSHOVER_URL, data=urllib.parse.urlencode(data).encode(), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            body = r.read().decode("utf-8", "replace")
            ok = json.loads(body).get("status") == 1
            return ok, body
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:300]}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def notify(event, job):
    if not SETTINGS["pushover_enabled"] or not SETTINGS.get("notify_" + event):
        return
    src = os.path.basename(job.params["source"].rstrip("/"))
    r = job.result or {}
    if event == "done":
        title = f"FPKG fertig: {r.get('title') or src}"
        lines = [f"Version {r.get('version', '?')} · FW {r.get('fw', '?')} · {human(r.get('size'))}",
                 f"Dauer {fmt_eta(r.get('duration'))}" if r.get("duration") else "",
                 os.path.basename(r.get("pkg", ""))]
        if job.warnings:
            lines.append("Warnungen: " + " | ".join(job.warnings)[:400])
        prio = 0
    elif event == "failed":
        title, lines, prio = f"FPKG Fehler: {src}", [job.error or "unbekannter Fehler", f"Schritt: {job.step}"], 1
    else:
        title, lines, prio = f"FPKG wartet auf Freigabe: {src}", ["Erkennung prüfen und in der WebUI 'Bauen' klicken."], 0
    msg = "\n".join(x for x in lines if x)

    def _send():
        ok, info = send_pushover(title, msg, prio, SETTINGS.get("webui_url") or None)
        (LOG.info if ok else LOG.warning)("pushover %s job %s: %s", event, job.id, "ok" if ok else info)
    threading.Thread(target=_send, daemon=True).start()


# ---------------------------------------------------------------- watch folder
WATCH = {"last_scan": None, "error": None, "items": {}}
WATCH_FILE = os.path.join(DATA, "watch_state.json")
WATCH_PROCESSED = {}
WATCH_LOCK = threading.Lock()
WATCH_KICK = threading.Event()
INCOMPLETE = (".part", ".tmp", ".crdownload", ".!qb", ".jdtmp", ".download", ".partial")
RX_TITLE = re.compile(r"(PPSA|PPSB|CUSA)\d{5}", re.I)


def load_watch():
    try:
        if os.path.exists(WATCH_FILE):
            WATCH_PROCESSED.update(json.load(open(WATCH_FILE)))
    except Exception:
        LOG.exception("could not read watch_state.json")


def save_watch():
    try:
        tmp = WATCH_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(WATCH_PROCESSED, f)
        os.replace(tmp, WATCH_FILE)
    except Exception:
        LOG.exception("could not write watch_state.json")


def watch_candidates(wdir, fixdir):
    """Top-level entries of the inbox -> {key: [paths belonging to it]}."""
    groups = {}
    for e in sorted(os.scandir(wdir), key=lambda x: x.name.lower()):
        n = e.name
        if n.startswith((".", "_")) or n in JUNK or (fixdir and os.path.realpath(e.path) == os.path.realpath(fixdir)):
            continue
        if e.is_dir():
            groups[os.path.realpath(e.path)] = [e.path]
            continue
        ext = lower_ext(n)
        if ext in ARCHIVES:
            vols = archive_volumes(e.path)
            first = os.path.realpath(first_volume(vols))
            groups.setdefault(first, sorted(set(vols)))
        elif ext in IMAGES or ext == ".pkg":
            groups[os.path.realpath(e.path)] = [e.path]
        elif n.lower().endswith(INCOMPLETE):
            base = re.sub(r"\.(part|tmp|crdownload|!qb|jdtmp|download|partial)$", "", n, flags=re.I)
            groups.setdefault("incomplete:" + base, [e.path])
    return groups


def group_signature(paths):
    size, mt, count, incomplete = 0, 0.0, 0, False
    for p in paths:
        if os.path.isdir(p):
            for cur, _, files in os.walk(p):
                for f in files:
                    fp = os.path.join(cur, f)
                    try:
                        st = os.stat(fp)
                    except OSError:
                        continue
                    size += st.st_size
                    mt = max(mt, st.st_mtime)
                    count += 1
                    if f.lower().endswith(INCOMPLETE):
                        incomplete = True
        else:
            try:
                st = os.stat(p)
                size += st.st_size
                mt = max(mt, st.st_mtime)
                count += 1
            except OSError:
                incomplete = True
            if p.lower().endswith(INCOMPLETE):
                incomplete = True
    return (size, round(mt, 1), count), incomplete


def title_id_for(path, kind, real):
    m = RX_TITLE.search(os.path.basename(path.rstrip("/"))) or RX_TITLE.search(real)
    if m:
        return m.group(0).upper()
    try:
        if kind in ("folder", "exfat", "ffpfsc", "ffpkg"):
            _, out = cmd_out([CLI, "inspect", real], 120)
        elif kind == "pkg":
            _, out = cmd_out([CLI, "pkg-info", real], 120)
        else:
            return None
        m = RX_TITLE.search(out)
        return m.group(0).upper() if m else None
    except Exception:
        return None


def list_fixes(fixdir, tid=None):
    """Fix candidates in fixdir (folders and first volumes of archives), matching title ID first, then newest."""
    if not (fixdir and os.path.isdir(fixdir)):
        return []
    out = []
    for e in os.scandir(fixdir):
        if e.name.startswith(".") or e.name in JUNK:
            continue
        try:
            if e.is_dir():
                size = None
            elif e.is_file() and lower_ext(e.name) in ARCHIVES:
                vols = archive_volumes(e.path)
                if os.path.realpath(first_volume(vols)) != os.path.realpath(e.path):
                    continue
                size = sum(os.path.getsize(v) for v in vols)
            else:
                continue
            mtime = e.stat().st_mtime
        except OSError:
            continue
        m = RX_TITLE.search(e.name)
        out.append({"path": e.path, "name": e.name, "size": size, "mtime": mtime,
                    "title_id": m.group(0).upper() if m else None,
                    "match": bool(tid and tid.lower() in e.name.lower())})
    out.sort(key=lambda f: (not f["match"], -f["mtime"]))
    return out


def find_fix(fixdir, tid):
    hits = [f for f in list_fixes(fixdir, tid) if f["match"]]
    return hits[0]["path"] if hits else None


def watch_scan():
    if not SETTINGS["watch_enabled"]:
        WATCH["error"] = None
        return
    try:
        wdir = safe_path(SETTINGS["watch_dir"], must_exist=False)
        fixdir = safe_path(SETTINGS["watch_fix_dir"], must_exist=False) if SETTINGS["watch_fix_dir"] else None
    except JobError as e:
        WATCH["error"] = str(e)
        return
    os.makedirs(wdir, exist_ok=True)
    if fixdir:
        os.makedirs(fixdir, exist_ok=True)
    WATCH["error"] = None
    now = time.time()
    groups = watch_candidates(wdir, fixdir)
    stable_s = SETTINGS["watch_stable"]
    with WATCH_LOCK:
        items = WATCH["items"]
        for key in list(items):
            if key not in groups:
                items.pop(key)
        for key, paths in groups.items():
            name = os.path.basename(key.split(":", 1)[-1])
            it = items.setdefault(key, {"name": name, "sig": None, "since": now, "state": "wartet"})
            if key in WATCH_PROCESSED:
                it.update(state="verarbeitet", job=WATCH_PROCESSED[key].get("job"))
                continue
            if key.startswith("incomplete:"):
                it.update(state="lädt noch")
                continue
            sig, incomplete = group_signature(paths)
            if sig != it["sig"] or incomplete:
                it.update(sig=sig, since=now, state="lädt noch" if incomplete else "wartet auf Stillstand")
                continue
            if now - it["since"] < stable_s or now - sig[1] < stable_s:
                it["state"] = f"stabil in {int(max(stable_s - (now - it['since']), stable_s - (now - sig[1])))} s"
                continue
            try:
                kind, real = classify(key)
            except JobError as e:
                it.update(state="ignoriert: " + str(e)[:120])
                WATCH_PROCESSED[key] = {"job": None, "time": now, "ignored": str(e)[:200]}
                save_watch()
                LOG.info("watch: ignored %s (%s)", key, e)
                continue
            tid = title_id_for(key, kind, real)
            fix = find_fix(fixdir, tid)
            params = {
                "source": key, "fix": fix, "out": safe_path(DEF_OUT, must_exist=False),
                "work": safe_path(DEF_WORK, must_exist=False), "preset": SETTINGS["watch_preset"],
                "keep_ampr": False, "keep_dump": False, "confirm": SETTINGS["watch_confirm"],
                "cleanup": SETTINGS["watch_cleanup"],
                "delete_archive": SETTINGS["watch_delete_archive"] and kind == "archive",
                "checksum": SETTINGS["checksum"], "origin": "watch", "watch_items": paths, "title_id": tid,
            }
            j = enqueue(params)
            WATCH_PROCESSED[key] = {"job": j.id, "time": now, "title_id": tid, "fix": fix}
            save_watch()
            it.update(state="eingereiht", job=j.id)
            LOG.info("watch: queued %s as job %s (title=%s fix=%s)", key, j.id, tid, fix)
            j.log(f"Automatisch aus dem Watch-Ordner eingereiht · Title-ID {tid or '?'} · Fix: {fix or 'keiner gefunden'}")
    WATCH["last_scan"] = now


def watch_after(job):
    if job.origin != "watch" or job.status != "done" or SETTINGS["watch_after"] != "move":
        return
    try:
        wdir = safe_path(SETTINGS["watch_dir"])
    except JobError:
        return
    dest = os.path.join(wdir, "_erledigt")
    os.makedirs(dest, exist_ok=True)
    for p in job.params.get("watch_items") or []:
        if os.path.exists(p):
            try:
                shutil.move(p, os.path.join(dest, os.path.basename(p)))
                job.log(f"Quelle verschoben nach {dest}: {os.path.basename(p)}")
            except OSError as e:
                job.warn(f"Konnte {p} nicht nach _erledigt verschieben: {e}")


def watcher():
    while True:
        try:
            watch_scan()
        except Exception as e:
            WATCH["error"] = f"{type(e).__name__}: {e}"
            LOG.exception("watch scan failed")
        WATCH_KICK.wait(max(5, SETTINGS["watch_interval"]))
        WATCH_KICK.clear()


def enqueue(params):
    os.makedirs(params["out"], exist_ok=True)
    os.makedirs(params["work"], exist_ok=True)
    j = Job(params)
    with QUEUE_CV:
        JOBS[j.id] = j
        ORDER.append(j.id)
        QUEUE_CV.notify_all()
    LOG.info("job %s: queued (%s) %s", j.id, params.get("origin", "manual"),
             json.dumps({k: ("••••" if k == "password" and v else v) for k, v in params.items()
                         if k != "watch_items"}, ensure_ascii=False))
    save_jobs()
    return j


# ---------------------------------------------------------------- archive passwords
def mask_cmd(cmd):
    return " ".join(("-p••••" if c.startswith("-p") and len(c) > 2 else (f'"{c}"' if " " in c else c)) for c in cmd)


def archive_encryption(archive):
    """-> (state, listing) with state in none|data|header|unknown; never prompts (empty -p, stdin closed)."""
    rc, out = cmd_out_stdin([SEVENZ, "l", "-slt", "-p", archive], 300)
    if rc != 0 and re.search(r"encrypted|wrong password", out, re.I):
        return "header", out
    if rc != 0:
        return "unknown", out
    return ("data" if "Encrypted = +" in out else "none"), out


def password_candidates(job_pw=None):
    c = []
    for pw in [job_pw or ""] + SETTINGS.get("archive_passwords", "").splitlines():
        pw = pw.strip()
        if pw and pw not in c:
            c.append(pw)
    return c


def find_password(archive, state, listing, candidates, log=lambda m: None):
    """Cheapest check that tells right from wrong: listing for header encryption,
    test of the smallest encrypted file otherwise."""
    if state == "header":
        for i, pw in enumerate(candidates, 1):
            rc, _ = cmd_out_stdin([SEVENZ, "l", f"-p{pw}", archive], 300)
            log(f"Passwort-Kandidat {i}/{len(candidates)}: {'passt' if rc == 0 else 'falsch'}")
            if rc == 0:
                return pw
        return None
    files, cur = [], {}
    for ln in listing.splitlines() + [""]:
        if not ln.strip():
            if cur.get("Encrypted") == "+" and cur.get("Folder") != "+" and cur.get("Path"):
                files.append((int(cur.get("Size") or 0), cur["Path"]))
            cur = {}
            continue
        if " = " in ln:
            k, v = ln.split(" = ", 1)
            cur[k.strip()] = v.strip()
    if not files:
        return candidates[0] if candidates else None
    small = min(files)[1]
    for i, pw in enumerate(candidates, 1):
        rc, out = cmd_out_stdin([SEVENZ, "t", f"-p{pw}", archive, small], 900)
        ok = rc == 0 and "Everything is Ok" in out
        log(f"Passwort-Kandidat {i}/{len(candidates)} (Test mit {os.path.basename(small)}): {'passt' if ok else 'falsch'}")
        if ok:
            return pw
    return None


# ---------------------------------------------------------------- progress parsing
RX_BAR = re.compile(r"^\[[^\]]*\]\s+(\d+(?:\.\d+)?)%\s*·\s*(.*)$")
RX_7Z = re.compile(r"^\s*(\d{1,3})%")
RX_DETAIL = re.compile(r"(Kraken level -?\d+:\s*\d+% of .+|data\s+\d+% \(\d+/\d+\): .+?(?= ->|$)|read \d+/\d+ \(\s*\d+%\))")
RX_DATA = re.compile(r"\bdata\s+(\d+)% \(\d+/\d+\)")
RX_LARGE = re.compile(r"processing large file: .+ \(([\d,]+) bytes\)")
RX_KRAKEN = re.compile(r"Kraken level -?\d+:\s*(\d+)% of ")
COMPRESS_END = 88.0  # overall % where the builder leaves the Kraken data phase


def parse_fpkg(job, line):
    """Progress from fpkg-cli output. Without a TTY the overall bar is printed rarely, so the long
    Kraken phase is interpolated from 'data NN%' lines and per-file Kraken percentages. True = log line."""
    bp = job.bp
    m = RX_BAR.match(line)
    if m:
        pct = float(m.group(1))
        rest = [x.strip() for x in m.group(2).split("·")]
        phase = re.sub(r"\s+\d+%$", "", rest[0]) if rest else ""
        eta = next((x.replace("left", "").replace("~", "").strip() for x in rest if "left" in x), "")
        extra = " · ".join(x for x in rest[1:] if "elapsed" not in x and "left" not in x and x)
        bp["bar"] = pct
        if "Kraken" in phase and bp["base"] is None:
            bp["base"] = pct
        if phase and phase != job.prog.get("phase"):
            job.prog["detail"] = ""
        job.set_progress(max(pct, job.prog["pct"] or 0) if pct < 100 else 100, phase or None,
                         extra or None, eta or job.eta_from(pct))
        return True
    m = RX_DETAIL.search(line)
    if m:
        job.prog["detail"] = m.group(1).strip()
    changed = False
    m = RX_DATA.search(line)
    if m:
        bp["data"], bp["large"], bp["lpct"], changed = int(m.group(1)) / 100, 0, 0.0, True
    m = RX_LARGE.search(line)
    if m:
        bp["large"], bp["lpct"], changed = int(m.group(1).replace(",", "")), 0.0, True
    m = RX_KRAKEN.search(line)
    if m:
        bp["lpct"], changed = int(m.group(1)) / 100, True
    if changed and bp["bar"] < COMPRESS_END:
        frac = bp["data"]
        if bp["total"] and bp["large"]:
            frac += bp["large"] * bp["lpct"] / bp["total"]
        base = bp["base"] if bp["base"] is not None else 2.5
        pct = max(job.prog["pct"] or 0, base + min(frac, 1.0) * (COMPRESS_END - base))
        job.set_progress(pct, None, None, job.eta_from(pct))
    return True


def parse_7z(job, line):
    m = RX_7Z.match(line)
    if m:
        pct = float(m.group(1))
        job.set_progress(pct, None, line[m.end():].strip(" -"), job.eta_from(pct))
        return False
    if re.match(r"^\d+M Scan", line.strip()):
        return False
    return True


# ---------------------------------------------------------------- jobs
PUBLIC = ("id", "created", "status", "step", "prog", "plan", "params", "result", "warnings", "error",
          "finished", "started", "phase", "origin")


class Job:
    def __init__(self, params):
        self.id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
        self.created = time.time()
        self.started = None
        self.status = "queued"
        self.step = "wartet"
        self.prog = {"pct": None, "phase": "", "detail": "", "eta": ""}
        self.plan = []
        self.params = params
        self.result = None
        self.warnings = []
        self.error = None
        self.finished = None
        self.phase = "prep"
        self.origin = params.get("origin", "manual")
        self._init_runtime()

    def _init_runtime(self):
        self.proc = None
        self.cancel_flag = False
        self.go = threading.Event()
        self.keep_work = False
        self.build_src = None
        self.fix_files = []
        self.reserve = 0
        self.src_bytes = None
        self.step_started = time.time()
        self.logpath = os.path.join(DATA, "logs", "jobs", self.id + ".log")
        self.work = os.path.join(self.params["work"], "job-" + self.id)

    def public(self):
        d = {k: getattr(self, k) for k in PUBLIC}
        if d["params"].get("password"):
            d["params"] = dict(d["params"], password="••••")
        return d

    # -- logging / progress
    def log(self, msg=""):
        with open(self.logpath, "a", encoding="utf-8") as f:
            f.write(msg + "\n")

    def set_plan(self, labels):
        done = {p["label"]: p["state"] for p in self.plan}
        self.plan = [{"label": l, "state": done.get(l, "todo")} for l in labels]

    def set_step(self, label):
        for p in self.plan:
            if p["state"] == "active":
                p["state"] = "done"
        hit = next((p for p in self.plan if p["label"] == label), None)
        if not hit:
            hit = {"label": label, "state": "todo"}
            self.plan.append(hit)
        hit["state"] = "active"
        self.step = label
        self.step_started = time.time()
        self.prog = {"pct": None, "phase": "", "detail": "", "eta": ""}
        self.log(f"\n== {time.strftime('%H:%M:%S')} {label}")
        LOG.info("job %s: %s", self.id, label)
        save_jobs()

    def set_progress(self, pct, phase=None, detail=None, eta=None):
        self.prog["pct"] = None if pct is None else max(0.0, min(100.0, pct))
        if phase is not None:
            self.prog["phase"] = phase
        if detail is not None:
            self.prog["detail"] = detail
        if eta is not None:
            self.prog["eta"] = eta

    def eta_from(self, pct):
        el = time.time() - self.step_started
        if pct and pct > 0.5:
            return fmt_eta(el * (100 - pct) / pct)
        return ""

    def check(self):
        if self.cancel_flag:
            raise Cancelled()

    def warn(self, msg):
        self.warnings.append(msg)
        self.log("WARNUNG: " + msg)
        LOG.warning("job %s: %s", self.id, msg)

    # -- processes
    def run(self, cmd, parser=None):
        self.check()
        self.bp = {"total": getattr(self, "src_bytes", None), "data": 0.0, "large": 0, "lpct": 0.0,
                   "base": None, "bar": 0.0}
        self.log("$ " + mask_cmd(cmd))
        LOG.debug("job %s exec: %s", self.id, mask_cmd(cmd))
        self.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     start_new_session=True)
        buf = b""
        with open(self.logpath, "ab") as lf:
            while True:
                chunk = self.proc.stdout.read1(65536)
                if not chunk:
                    break
                buf += chunk
                parts = re.split(rb"[\r\n\x08]+", buf)
                buf = parts.pop()
                for p in parts:
                    line = p.decode("utf-8", "replace").rstrip()
                    if not line.strip():
                        continue
                    keep = parser(self, line) if parser else True
                    if keep:
                        lf.write(line.encode("utf-8") + b"\n")
                lf.flush()
            if buf.strip():
                lf.write(buf.rstrip() + b"\n")
        rc = self.proc.wait()
        self.proc = None
        self.check()
        if rc != 0:
            raise JobError(f"Befehl fehlgeschlagen (Exit {rc}): {os.path.basename(cmd[0])} {cmd[1] if len(cmd) > 1 else ''}"
                           " – Details im Log")
        return rc

    def capture(self, cmd, log=True):
        self.check()
        r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        out = r.stdout.decode("utf-8", "replace")
        if log:
            self.log(out.rstrip())
        return r.returncode, out

    def copy_tree(self, src, dst):
        total = max(dir_size(src), 1)
        done = 0
        t0 = time.time()
        last = 0.0
        for cur, dirs, files in os.walk(src):
            rel = os.path.relpath(cur, src)
            tdir = os.path.join(dst, rel) if rel != "." else dst
            os.makedirs(tdir, exist_ok=True)
            for f in files:
                s, d = os.path.join(cur, f), os.path.join(tdir, f)
                if os.path.islink(s):
                    os.symlink(os.readlink(s), d)
                    continue
                with open(s, "rb") as fi, open(d, "wb") as fo:
                    while True:
                        self.check()
                        b = fi.read(COPY_CHUNK)
                        if not b:
                            break
                        fo.write(b)
                        done += len(b)
                        now = time.time()
                        if now - last > 0.7:
                            last = now
                            pct = done * 100 / total
                            spd = done / max(now - t0, 0.001)
                            self.set_progress(pct, None, f"{human(done)} / {human(total)} · {human(spd)}/s",
                                              fmt_eta((total - done) / spd if spd else None))
                shutil.copystat(s, d, follow_symlinks=False)
        self.set_progress(100)

    def sevenz(self, archive, dest, need_build=False):
        """Extract; checks free space first (unpacked size, plus build temp if the content is built later)."""
        state, listing = archive_encryption(archive)
        pw = ""
        if state in ("header", "data"):
            cands = password_candidates(self.params.get("password"))
            self.log(f"Archiv ist passwortgeschützt ({'Dateinamen verschlüsselt' if state == 'header' else 'Inhalt verschlüsselt'})"
                     f" · {len(cands)} Passwort-Kandidat(en)")
            if not cands:
                raise JobError(f"{os.path.basename(archive)} ist passwortgeschützt. Passwort im Feld „Archiv-Passwort“ "
                               "eintragen oder unter Einstellungen → Archiv-Passwörter hinterlegen.")
            pw = find_password(archive, state, listing, cands, self.log)
            if pw is None:
                raise JobError(f"Kein passendes Passwort für {os.path.basename(archive)} – "
                               f"{len(cands)} Kandidat(en) probiert.")
            self.log("Passwort gefunden.")
        elif state == "unknown":
            self.log("Archiv-Info konnte nicht gelesen werden: " + listing.strip().splitlines()[-1][:200] if listing.strip() else "")
        rc, out = cmd_out_stdin([SEVENZ, "l", f"-p{pw}", archive], 300)
        m = re.search(r"^\S+ \S+\s+(\d+)\s+(\d+)?\s*\d+ files", out, re.M) if rc == 0 else None
        if m:
            unpacked = int(m.group(1))
            need = int(unpacked * (1.6 if need_build else 1.05))
            free = free_for(dest, self)
            self.reserve = need
            self.log(f"Archiv entpackt: {human(unpacked)} · benötigt im Arbeitsordner ~{human(need)} · frei {human(free)}")
            if free is not None and free < need:
                raise JobError(f"Zu wenig Platz im Arbeitsordner: braucht ~{human(need)}, frei {human(free)}. "
                               "Unter 'Erweitert' einen größeren Arbeitsordner wählen (z. B. auf dem Array).")
        self.run([SEVENZ, "x", "-y", "-bso0", "-bse1", "-bsp1", f"-p{pw}", f"-o{dest}", archive], parse_7z)
        self.set_progress(100)

    def unwrap_nested(self, dst):
        """Classify extracted content. Some releases (e.g. DUPLEX) put the real archive inside the split RAR:
        extract such inner archives up to MAX_NEST levels. They live in the work dir and are deleted right after
        extraction, so the space is free again for the build."""
        kind, inner = classify(dst)
        self.log(f"Im Archiv erkannt: {kind} -> {inner}")
        level = 1
        while kind == "archive":
            if level > MAX_NEST:
                raise JobError(f"Mehr als {MAX_NEST} ineinander verpackte Archive – Quelle prüfen")
            level += 1
            work = os.path.realpath(self.work) + os.sep
            vols = archive_volumes(inner)
            if not all(os.path.realpath(v).startswith(work) for v in vols):
                raise JobError(f"Inneres Archiv liegt außerhalb des Arbeitsordners: {inner}")
            labels = [x["label"] for x in self.plan]
            if "Inneres Archiv entpacken" not in labels:
                at = labels.index("Quelle entpacken") + 1 if "Quelle entpacken" in labels else len(labels)
                self.set_plan(labels[:at] + ["Inneres Archiv entpacken"] + labels[at:])
            self.set_step("Inneres Archiv entpacken")
            self.log(f"Archiv enthält ein weiteres Archiv (Ebene {level}): " + ", ".join(os.path.basename(v) for v in vols)
                     + f" ({human(sum(os.path.getsize(v) for v in vols))})")
            nxt = os.path.join(self.work, f"src{level}")
            self.sevenz(inner, nxt, need_build=True)
            for v in vols:
                try:
                    os.remove(v)
                except OSError as e:
                    self.warn(f"Konnte inneres Archiv {v} nicht löschen: {e}")
            self.log("Inneres Archiv gelöscht (lag im Arbeitsordner).")
            strip_junk(nxt)
            kind, inner = classify(nxt)
            self.log(f"Im inneren Archiv erkannt: {kind} -> {inner}")
        return kind, inner

    # -- pipeline
    def prepare(self):
        p = self.params
        self.started = time.time()
        os.makedirs(self.work, exist_ok=True)
        tmp = os.path.join(self.work, "tmp")
        os.makedirs(tmp, exist_ok=True)
        self.log(f"FPKG WebUI {APP_VERSION} · Job {self.id}\nQuelle: {p['source']}\nFix:    {p.get('fix') or '-'}\n"
                 f"Ziel:   {p['out']}\nArbeit: {self.work}\n"
                 f"Optionen: preset={p.get('preset')} anhalten={p.get('confirm')} aufräumen={p.get('cleanup')} "
                 f"archiv_löschen={p.get('delete_archive')} keep_ampr={p.get('keep_ampr')} keep_dump={p.get('keep_dump')}")
        self.log(f"Freier Platz Arbeitsordner: {human(disk(self.work)['free'])} · Ausgabe: {human(disk(p['out'])['free'])}")
        kind, src = classify(p["source"])
        self.log(f"Erkannt: {kind} -> {src}")
        fix = p.get("fix")
        plan = []
        sums = find_checksums(src) if p.get("checksum", True) and os.path.isfile(src) else {}
        if sums:
            plan.append("Prüfsumme")
        if kind == "archive":
            plan.append("Quelle entpacken")
        elif kind == "pkg":
            plan.append("FPKG entpacken")
        elif fix and kind == "exfat":
            plan.append("App aus exFAT kopieren")
        elif fix and kind == "folder":
            plan.append("App-Ordner kopieren")
        if fix:
            plan += ["Fix übernehmen"]
        plan += ["Quelle prüfen"] + (["Freigabe"] if p.get("confirm") else []) + ["FPKG bauen", "Paket prüfen"]
        if p.get("cleanup"):
            plan.append("Aufräumen")
        self.set_plan(plan)

        if sums:
            self.set_step("Prüfsumme")
            self.verify_checksums(src, kind, sums)

        in_work = False
        if kind == "archive":
            self.set_step("Quelle entpacken")
            vols = archive_volumes(src)
            self.log("Archiv-Teile: " + ", ".join(os.path.basename(v) for v in vols)
                     + f" ({human(sum(os.path.getsize(v) for v in vols))})")
            dst = os.path.join(self.work, "src")
            self.sevenz(src, dst, need_build=True)
            strip_junk(dst)
            inner_kind, inner = self.unwrap_nested(dst)
            if p.get("delete_archive"):
                self.keep_work = True
                for v in vols:
                    try:
                        os.remove(v)
                        self.log(f"Quellarchiv gelöscht: {v}")
                        LOG.info("job %s: deleted source archive %s", self.id, v)
                    except OSError as e:
                        self.warn(f"Konnte {v} nicht löschen: {e}")
            kind, src, in_work = inner_kind, inner, True
            # adjust plan to the real inner type
            np_ = [x["label"] for x in self.plan]
            if kind == "exfat" and fix:
                np_.insert(np_.index("Quelle entpacken") + 1, "App aus exFAT kopieren")
            if kind == "pkg":
                np_.insert(np_.index("Quelle entpacken") + 1, "FPKG entpacken")
            self.set_plan(np_)

        app = build_src = None
        if kind == "folder":
            app = build_src = src
            if fix and not in_work:
                self.set_step("App-Ordner kopieren")
                app = build_src = os.path.join(self.work, "app")
                self.copy_tree(src, app)
        elif kind == "exfat":
            if fix:
                self.set_step("App aus exFAT kopieren")
                app = build_src = self.extract_exfat(src)
            else:
                build_src = src
        elif kind in ("ffpfsc", "ffpkg"):
            if fix:
                raise JobError(f".{kind}-Images lassen sich nicht patchen. Ohne Fix bauen oder als Ordner/.exfat bereitstellen.")
            build_src = src
        elif kind == "pkg":
            self.set_step("FPKG entpacken")
            dst = os.path.join(self.work, "app")
            self.run([CLI, "pkg-extract", src, "--output", dst, "--temp", tmp], parse_fpkg)
            app = build_src = find_app_dir(dst) or dst

        fix_files = []
        if fix:
            self.set_step("Fix übernehmen")
            if os.path.isfile(fix):
                fdst = os.path.join(self.work, "fix")
                self.sevenz(fix, fdst)
                strip_junk(fdst)
                fix = fdst
            froot = fix_root(fix)
            fix_files = rel_files(froot)
            if not fix_files:
                raise JobError("Fix enthält keine Dateien")
            self.log(f"Fix-Wurzel: {froot}")
            new = 0
            for rf in fix_files:
                exists = os.path.exists(os.path.join(app, rf))
                new += not exists
                self.log(("ERSETZT  " if exists else "NEU      ") + rf)
            self.log(f"{len(fix_files) - new} ersetzt, {new} neu")
            if len(fix_files) > 2 and new > len(fix_files) / 2:
                self.warn("Mehr als die Hälfte der Fix-Dateien ist NEU – die Ordnerebene des Fixes passt evtl. nicht.")
            for i, rf in enumerate(fix_files, 1):
                s, d = os.path.join(froot, rf), os.path.join(app, rf)
                os.makedirs(os.path.dirname(d), exist_ok=True)
                if os.path.isdir(d):
                    raise JobError(f"Im App-Ordner ist {rf} ein Ordner, im Fix eine Datei")
                shutil.copy2(s, d)
                self.set_progress(i * 100 / len(fix_files), None, rf, "")
            self.log("Fix übernommen.")

        if app and (in_work or fix or kind == "pkg"):
            strip_junk(app)

        self.set_step("Quelle prüfen")
        rc, out = self.capture([CLI, "inspect", build_src])
        m = re.search(r"Content ID (\S+) .*? version (\S+)", out)
        if m:
            self.log(f"-> {m.group(1)} v{m.group(2)}")
        m = re.search(r"Size: .*?\(([\d,]+) byte\)", out)
        self.src_bytes = int(m.group(1).replace(",", "")) if m else None
        if rc != 0 or "eboot.bin: no" in out or "sce_sys: no" in out:
            raise JobError("inspect: Quelle ungültig (sce_sys oder eboot.bin fehlt?) – siehe Log")

        self.build_src, self.fix_files, self.tmp = build_src, fix_files, tmp
        self.reserve = int((self.src_bytes or 0) * 0.6)
        if p.get("confirm"):
            self.set_step("Freigabe")
            self.status = "waiting"
            self.step = "Wartet auf Freigabe – Log prüfen, dann 'Bauen'"
            self.log("== Angehalten. Erkennung und Fix-Abgleich oben prüfen, dann in der Oberfläche 'Bauen' klicken.")
            notify("waiting", self)
        else:
            for x in self.plan:
                if x["state"] == "active":
                    x["state"] = "done"
            self.status, self.step = "ready", "bereit zum Bauen"
            self.log("== Vorbereitet, wartet auf freien Build-Platz.")
        save_jobs()

    def build(self):
        p = self.params
        build_src, fix_files, tmp = self.build_src, self.fix_files, self.tmp
        self.phase = "build"
        self.set_step("FPKG bauen")
        stage = os.path.join(p["out"], ".building-" + self.id)
        os.makedirs(stage, exist_ok=True)
        cmd = [CLI, "build", "--source", build_src, "--output", stage, "--temp", tmp,
               "--no-sony-sdk", "--no-sleep-guard", "--preset", p.get("preset") or "standard"]
        if p.get("keep_ampr"):
            cmd.append("--keep-ampr")
        if p.get("keep_dump"):
            cmd.append("--keep-dump-leftovers")
        try:
            self.run(cmd, parse_fpkg)
            pkgs = [f for f in os.listdir(stage) if f.lower().endswith(".pkg")]
            if not pkgs:
                raise JobError("Build lief durch, aber kein .pkg gefunden")
            name = pkgs[0]
            final = os.path.join(p["out"], name)
            if os.path.exists(final):
                base, ext = os.path.splitext(name)
                final = os.path.join(p["out"], f"{base}-{self.id}{ext}")
                self.warn(f"{name} existierte schon – neues Paket heißt {os.path.basename(final)}")
            os.rename(os.path.join(stage, name), final)
        finally:
            shutil.rmtree(stage, ignore_errors=True)
        try:
            os.chown(final, PUID, PGID)
        except OSError:
            pass

        self.set_step("Paket prüfen")
        rc, info = self.capture([CLI, "pkg-info", final])
        res = {"pkg": final, "size": os.path.getsize(final)}
        for key, rx in (("content_id", r"Content ID:\s*(\S+)"), ("version", r"contentVersion:\s*(\S+)"),
                        ("fw", r"Required system software:\s*(\S+)"), ("title", r"Title:\s*(.+)")):
            m = re.search(rx, info)
            if m:
                res[key] = m.group(1).strip()
        self.set_progress(40)
        if fix_files:
            cmd = [CLI, "pkg-list", final]
            for rf in fix_files:
                cmd += ["--include", rf]
            rc, lst = self.capture(cmd)
            lines = [ln.rstrip() for ln in lst.splitlines()]
            missing = [rf for rf in fix_files if not any(ln.endswith(" " + rf) or ln == rf for ln in lines)]
            res["fix_missing"] = missing
            if missing:
                self.warn("Fix-Dateien NICHT im Paket (vom Builder entfernt): " + ", ".join(missing))
            else:
                self.log("Alle Fix-Dateien sind im Paket.")
        self.set_progress(70)
        vcmd = [CLI, "verify", final] + (["--full"] if p.get("full_verify") else [])
        self.log("Vollprüfung (--full) läuft …" if p.get("full_verify") else "Schnellprüfung …")
        rc, vout = self.capture(vcmd)
        res["verify"] = rc == 0
        res["verify_mode"] = "full" if p.get("full_verify") else "quick"
        m = RX_PASSED.search(vout)
        record_check(final, res["verify_mode"], rc == 0 and bool(m),
                     f"{m.group(2)} Prüfungen bestanden ({m.group(3)})" if m else "siehe Auftrags-Log", 0)
        if rc != 0:
            self.warn("verify meldet Fehler – Log prüfen")
        self.set_progress(100)
        res["duration"] = time.time() - self.started
        self.result = res

        if p.get("cleanup"):
            self.set_step("Aufräumen")
            shutil.rmtree(self.work, ignore_errors=True)
            self.log(f"Arbeitsordner gelöscht: {self.work}")
        else:
            chown_tree(self.work)
        for x in self.plan:
            x["state"] = "done"
        self.reserve = 0

    def verify_checksums(self, src, kind, sums):
        files = archive_volumes(src) if kind == "archive" else [src]
        names = {os.path.basename(f): f for f in files}
        todo = []
        for n, f in names.items():
            entry = sums.get(n.lower()) or (sums.get("*") if len(files) == 1 else None)
            if entry:
                todo.append((f, entry))
            else:
                self.log(f"keine Prüfsumme für {n}")
        if not todo:
            self.warn("Prüfsummendatei gefunden, aber kein passender Eintrag für die Quelldatei(en)")
            return
        total = sum(os.path.getsize(f) for f, _ in todo)
        done, t0, last = 0, time.time(), 0.0
        for f, (algo, want, origin) in todo:
            h = hashlib.sha256() if algo == "sha256" else hashlib.md5() if algo == "md5" else None
            crc = 0
            with open(f, "rb") as fh:
                while True:
                    self.check()
                    b = fh.read(COPY_CHUNK)
                    if not b:
                        break
                    if h:
                        h.update(b)
                    else:
                        crc = zlib.crc32(b, crc)
                    done += len(b)
                    now = time.time()
                    if now - last > 0.7:
                        last = now
                        spd = done / max(now - t0, 0.001)
                        self.set_progress(done * 100 / max(total, 1), None,
                                          f"{os.path.basename(f)} · {human(done)} / {human(total)} · {human(spd)}/s",
                                          fmt_eta((total - done) / spd if spd else None))
            got = h.hexdigest() if h else f"{crc & 0xffffffff:08x}"
            if got.lower() != want.lower():
                raise JobError(f"Prüfsumme FALSCH für {os.path.basename(f)} ({algo}): erwartet {want}, ist {got} "
                               f"(Quelle: {os.path.basename(origin)}). Download ist beschädigt.")
            self.log(f"Prüfsumme OK: {os.path.basename(f)} ({algo} aus {os.path.basename(origin)})")
        self.set_progress(100)

    def extract_exfat(self, image):
        try:
            img = ExfatImage(image)
        except (ExfatError, OSError) as e:
            raise JobError(f"exFAT-Image nicht lesbar: {e}")
        try:
            found = img.find_app_dir()
            if not found:
                raise JobError("Im exFAT-Image keinen Ordner mit sce_sys + eboot.bin gefunden")
            rel, node = found
            total = img.size_of(node)
            self.log(f"App-Ordner im Image: /{rel}  ({human(total)}, Volume-Offset {img.base}, Cluster {img.csize})")
            free = free_for(self.work, self)
            self.reserve = int(total * 1.6)
            if free is not None and free < total * 1.6:
                raise JobError(f"Zu wenig Platz im Arbeitsordner: braucht ~{human(total * 1.6)} (Kopie + Build-Temp), "
                               f"frei {human(free)}. Unter 'Erweitert' einen größeren Arbeitsordner wählen.")
            dst = os.path.join(self.work, "app")
            t0, last = time.time(), [0.0]

            def prog(done, tot):
                if self.cancel_flag:
                    raise Cancelled()
                now = time.time()
                if now - last[0] > 0.7:
                    last[0] = now
                    spd = done / max(now - t0, 0.001)
                    self.set_progress(done * 100 / max(tot, 1), None,
                                      f"{human(done)} / {human(tot)} · {human(spd)}/s",
                                      fmt_eta((tot - done) / spd if spd else None))

            img.extract(node, dst, prog)
            self.set_progress(100)
            self.log(f"{human(total)} kopiert nach {dst} in {fmt_eta(time.time() - t0)}")
            return dst
        except ExfatError as e:
            raise JobError(str(e))
        finally:
            img.close()

    def cancel(self):
        LOG.info("job %s: cancel requested", self.id)
        self.cancel_flag = True
        self.go.set()
        pr = self.proc
        if pr and pr.poll() is None:
            try:
                os.killpg(pr.pid, signal.SIGTERM)
                for _ in range(10):
                    if pr.poll() is not None:
                        break
                    time.sleep(0.5)
                if pr.poll() is None:
                    os.killpg(pr.pid, signal.SIGKILL)
            except OSError:
                pass


SAVE_LOCK = threading.Lock()


def save_jobs():
    try:
        with LOCK:
            data = [JOBS[i].public() for i in ORDER[-200:]]  # passwords are masked here
        with SAVE_LOCK:
            tmp = os.path.join(DATA, "jobs.json.tmp")
            with open(tmp, "w") as f:
                json.dump(data, f)
            os.replace(tmp, os.path.join(DATA, "jobs.json"))
    except Exception:
        LOG.exception("save_jobs failed")


def load_jobs():
    path = os.path.join(DATA, "jobs.json")
    if not os.path.exists(path):
        return
    try:
        for d in json.load(open(path)):
            j = Job.__new__(Job)
            for k in PUBLIC:
                setattr(j, k, d.get(k))
            j.prog = j.prog or {"pct": None, "phase": "", "detail": "", "eta": ""}
            j.plan = j.plan or []
            j.warnings = j.warnings or []
            j._init_runtime()
            if j.status in ("queued", "running", "waiting", "ready"):
                j.status, j.error, j.step = "failed", "Container wurde neu gestartet", "Fehler"
            JOBS[j.id] = j
            ORDER.append(j.id)
        LOG.info("loaded %d jobs from history", len(ORDER))
    except Exception:
        LOG.exception("could not load jobs.json")


def finish(job, exc=None):
    """Common end handling for both phases. exc=None means success of the build phase."""
    if exc is None:
        job.status, job.step = "done", "fertig"
        LOG.info("job %s: done -> %s", job.id, (job.result or {}).get("pkg"))
    elif isinstance(exc, Cancelled):
        job.status, job.step = "cancelled", "abgebrochen"
        job.log("\n== abgebrochen")
        LOG.info("job %s: cancelled", job.id)
        cleanup_after_fail(job)
    else:
        job.status, job.error, job.step = "failed", str(exc), "Fehler"
        job.log("\nFEHLER: " + str(exc))
        if not isinstance(exc, JobError):
            job.log("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
            LOG.error("job %s: unexpected error: %r", job.id, exc)
        else:
            LOG.error("job %s: %s", job.id, exc)
        cleanup_after_fail(job)
    for x in job.plan:
        if x["state"] == "active":
            x["state"] = "failed" if job.status != "done" else "done"
    job.reserve = 0
    job.finished = time.time()
    try:
        watch_after(job)
    except Exception:
        LOG.exception("watch_after failed")
    save_jobs()
    if job.status in ("done", "failed"):
        notify(job.status, job)


def building_busy():
    return any(JOBS[i].phase == "build" and JOBS[i].status == "running" for i in ORDER)


def prep_worker():
    """Phase 1: checksum, extract, copy, fix, inspect. Runs ahead of the builder when overlap is enabled."""
    while True:
        try:
            with QUEUE_CV:
                while True:
                    nxt = next((JOBS[i] for i in ORDER if JOBS[i].status == "queued"), None)
                    ready = any(JOBS[i].status == "ready" for i in ORDER)
                    pending = ready or any(JOBS[i].status == "waiting" for i in ORDER)
                    # overlap: prepare at most one job ahead of the running build
                    if nxt and ((SETTINGS.get("overlap", True) and not ready) or (not building_busy() and not pending)):
                        break
                    QUEUE_CV.wait(5)
                nxt.status, nxt.phase = "running", "prep"
            LOG.info("job %s: prepare source=%s fix=%s", nxt.id, nxt.params["source"], nxt.params.get("fix"))
            save_jobs()
            try:
                nxt.prepare()
            except BaseException as e:  # noqa: B902 - includes Cancelled
                finish(nxt, e)
            with QUEUE_CV:
                QUEUE_CV.notify_all()
        except Exception:
            LOG.exception("prep worker crashed, continuing")
            time.sleep(1)


def build_worker():
    """Phase 2: fpkg-cli build + package checks. One build at a time."""
    while True:
        try:
            with QUEUE_CV:
                while True:
                    nxt = next((JOBS[i] for i in ORDER if JOBS[i].status == "ready"), None)
                    if nxt:
                        break
                    QUEUE_CV.wait(5)
                nxt.status = "running"
            save_jobs()
            try:
                nxt.build()
                finish(nxt)
            except BaseException as e:  # noqa: B902
                finish(nxt, e)
            with QUEUE_CV:
                QUEUE_CV.notify_all()
        except Exception:
            LOG.exception("build worker crashed, continuing")
            time.sleep(1)


def cleanup_after_fail(j):
    if j.keep_work:
        j.log(f"Arbeitsordner bleibt erhalten (Quellarchiv wurde gelöscht): {j.work}")
        chown_tree(j.work)
    elif j.params.get("cleanup"):
        shutil.rmtree(j.work, ignore_errors=True)
        j.log(f"Arbeitsordner gelöscht: {j.work}")


# ---------------------------------------------------------------- package checks
CHECKS_FILE = os.path.join(DATA, "checks.json")
CHECK_RESULTS = {}          # path -> {"size":..,"mtime":..,"quick":{..},"full":{..}}
CHECKS = {}                 # id -> check item (current + recent)
CHECK_ORDER = []
CHECK_CV = threading.Condition()
RX_PASSED = re.compile(r"(Quick|Full) content check passed all (\d+) checks \(([^)]*)\)")


def load_checks():
    try:
        if os.path.exists(CHECKS_FILE):
            CHECK_RESULTS.update(json.load(open(CHECKS_FILE)))
    except Exception:
        LOG.exception("could not read checks.json")


def save_checks():
    try:
        tmp = CHECKS_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(CHECK_RESULTS, f)
        os.replace(tmp, CHECKS_FILE)
    except Exception:
        LOG.exception("could not write checks.json")


def record_check(path, mode, ok, summary, duration):
    try:
        st = os.stat(path)
    except OSError:
        return
    r = CHECK_RESULTS.get(path)
    if not r or r.get("size") != st.st_size or abs(r.get("mtime", 0) - st.st_mtime) > 1:
        r = {"size": st.st_size, "mtime": st.st_mtime}
    r[mode] = {"ok": ok, "time": time.time(), "summary": summary, "duration": duration}
    CHECK_RESULTS[path] = r
    save_checks()


def check_result(path, st):
    r = CHECK_RESULTS.get(path)
    if r and r.get("size") == st.st_size and abs(r.get("mtime", 0) - st.st_mtime) <= 1:
        return {k: r[k] for k in ("quick", "full") if k in r}
    return {}


def pkg_info(path):
    rc, out = cmd_out([CLI, "pkg-info", path], 120)
    info = {"ok": rc == 0}
    for key, rx in (("title", r"Title:\s*(.+)"), ("content_id", r"Content ID:\s*(\S+)"),
                    ("version", r"contentVersion:\s*(\S+)"), ("fw", r"Required system software:\s*(\S+)"),
                    ("sdk", r"^\s*SDK:\s*(.+)$"), ("type", r"Package type:\s*(.+)"),
                    ("image", r"Image mode:\s*(.+)")):
        m = re.search(rx, out, re.M)
        if m:
            info[key] = m.group(1).strip()
    info["fw_label"] = fw_label(info.get("fw"))
    if rc != 0:
        info["error"] = out.strip().splitlines()[-1][:300] if out.strip() else f"rc={rc}"
    return info


def fw_label(v):
    m = re.match(r"0x([0-9a-fA-F]{2})([0-9a-fA-F]{2})", v or "")
    return f"{int(m.group(1), 16)}.{int(m.group(2), 16):02d}" if m else (v or "?")


def list_packages(d):
    rp = safe_path(d)
    items = []
    for e in sorted(os.scandir(rp), key=lambda x: x.name.lower()):
        if e.is_file() and e.name.lower().endswith(".pkg") and not e.name.startswith("."):
            st = e.stat()
            items.append({"path": e.path, "name": e.name, "size": st.st_size, "mtime": st.st_mtime,
                          "checks": check_result(e.path, st)})
    items.sort(key=lambda x: -x["mtime"])
    return {"dir": rp, "items": items}


def enqueue_check(path, mode):
    path = safe_path(path)
    if not path.lower().endswith(".pkg"):
        raise JobError("Nur .pkg-Dateien können geprüft werden")
    for c in CHECKS.values():
        if c["path"] == path and c["mode"] == mode and c["status"] in ("queued", "running"):
            return c
    c = {"id": uuid.uuid4().hex[:8], "path": path, "name": os.path.basename(path), "mode": mode,
         "status": "queued", "created": time.time(), "started": None, "finished": None,
         "size": os.path.getsize(path), "read": 0, "summary": "", "output": "", "ok": None}
    with CHECK_CV:
        CHECKS[c["id"]] = c
        CHECK_ORDER.append(c["id"])
        while len(CHECK_ORDER) > 30:
            old = CHECK_ORDER.pop(0)
            if CHECKS.get(old, {}).get("status") not in ("queued", "running"):
                CHECKS.pop(old, None)
        CHECK_CV.notify_all()
    LOG.info("check %s queued: %s %s", c["id"], mode, path)
    return c


def run_check(c):
    cmd = [CLI, "verify", c["path"]] + (["--full"] if c["mode"] == "full" else [])
    c["status"], c["started"] = "running", time.time()
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
    c["_proc"] = proc
    reader = threading.Thread(target=lambda: c.__setitem__("output", proc.stdout.read().decode("utf-8", "replace")),
                              daemon=True)
    reader.start()
    base = None
    while proc.poll() is None:
        try:
            rchar = int(re.search(r"rchar: (\d+)", open(f"/proc/{proc.pid}/io").read()).group(1))
            base = rchar if base is None else base
            c["read"] = rchar
        except Exception:
            pass
        time.sleep(0.5)
    reader.join(5)
    rc = proc.returncode
    out = c["output"]
    m = RX_PASSED.search(out)
    c["ok"] = rc == 0 and bool(m)
    if c.get("cancelled"):
        c["status"], c["summary"] = "cancelled", "abgebrochen"
    else:
        fails = [ln.strip() for ln in out.splitlines() if re.search(r"fail|error|mismatch|invalid", ln, re.I)]
        c["summary"] = (f"{m.group(2)} Prüfungen bestanden ({m.group(3)})" if m and c["ok"]
                        else (fails[0][:300] if fails else (out.strip().splitlines() or ["kein Ergebnis"])[-1][:300]))
        c["status"] = "ok" if c["ok"] else "failed"
        record_check(c["path"], c["mode"], c["ok"], c["summary"], time.time() - c["started"])
    c["finished"] = time.time()
    c.pop("_proc", None)
    LOG.info("check %s %s: %s", c["id"], c["status"], c["summary"])


def check_worker():
    while True:
        with CHECK_CV:
            while True:
                nxt = next((CHECKS[i] for i in CHECK_ORDER if CHECKS.get(i, {}).get("status") == "queued"), None)
                if nxt:
                    break
                CHECK_CV.wait(5)
        try:
            run_check(nxt)
        except Exception as e:
            nxt.update(status="failed", summary=f"{type(e).__name__}: {e}", finished=time.time())
            LOG.exception("check failed")


def public_check(c):
    return {k: v for k, v in c.items() if not k.startswith("_") and k != "output"} | {
        "output": c["output"][-6000:] if c["status"] not in ("queued", "running") else ""}


# ---------------------------------------------------------------- diagnostics
_INFO_CACHE = {"t": 0, "v": None}


def cmd_out_stdin(cmd, timeout=60):
    try:
        r = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           timeout=timeout)
        return r.returncode, r.stdout.decode("utf-8", "replace").strip()
    except Exception as e:
        return -1, f"{type(e).__name__}: {e}"


def cmd_out(cmd, timeout=60):
    try:
        r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
        return r.returncode, r.stdout.decode("utf-8", "replace").strip()
    except Exception as e:
        return -1, f"{type(e).__name__}: {e}"


def fpkg_info(force=False):
    if force or not _INFO_CACHE["v"] or time.time() - _INFO_CACHE["t"] > 600:
        _INFO_CACHE["v"] = cmd_out([CLI, "info", "--lang", "en"])
        _INFO_CACHE["t"] = time.time()
    return _INFO_CACHE["v"]


def meminfo():
    try:
        d = {}
        for ln in open("/proc/meminfo"):
            k, v = ln.split(":", 1)
            d[k] = int(v.strip().split()[0]) * 1024
        return {"total": d.get("MemTotal"), "available": d.get("MemAvailable")}
    except Exception:
        return {}


def diag():
    rc, info = fpkg_info()
    _, z = cmd_out([SEVENZ], 15)
    roots_info = []
    for r in roots():
        dk = disk(r)
        roots_info.append({"path": r, "label": root_label(r), "free": dk["free"], "total": dk["total"],
                           "writable": os.access(r, os.W_OK)})
    with LOCK:
        counts = {}
        for i in ORDER:
            counts[JOBS[i].status] = counts.get(JOBS[i].status, 0) + 1
    return {
        "app_version": APP_VERSION,
        "uptime": time.time() - STARTED,
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "cpus": os.cpu_count(),
        "memory": meminfo(),
        "fpkg_cli_rc": rc,
        "fpkg_cli_info": info,
        "sevenzip": next((ln for ln in z.splitlines() if "7-Zip" in ln), z[:200]),
        "roots": roots_info,
        "config": {"BROWSE_ROOTS": ROOTS_SPEC, "DEFAULT_OUT": DEF_OUT, "DEFAULT_WORK": DEF_WORK,
                   "DATA_DIR": DATA, "PUID": PUID, "PGID": PGID, "PORT": PORT,
                   "LOG_LEVEL": ENV("LOG_LEVEL", "INFO"), "TZ": ENV("TZ", "")},
        "jobs": counts,
        "settings": {k: (f"•••• ({len(v.splitlines())} Einträge)" if k == "archive_passwords" and v else v)
                     for k, v in public_settings().items()},
        "watch": {"last_scan": WATCH["last_scan"], "error": WATCH["error"], "processed": len(WATCH_PROCESSED)},
    }


def selftest():
    checks = []
    rc, info = fpkg_info(force=True)
    checks.append({"name": "fpkg-cli startet", "ok": rc == 0, "detail": info.splitlines()[0] if info else ""})
    checks.append({"name": "LibProsperoPkg-Engine geladen", "ok": "[OK] LibProsperoPkg" in info, "detail": ""})
    checks.append({"name": "PS5 Debug-Keys bereit", "ok": "[OK] PS5 debug keys" in info, "detail": ""})
    rc, z = cmd_out([SEVENZ, "i"], 15)
    checks.append({"name": "7-Zip vorhanden (inkl. RAR)", "ok": rc == 0 and " Rar5 " in z,
                   "detail": next((ln for ln in z.splitlines() if "7-Zip" in ln), "")})
    rs = roots()
    checks.append({"name": "Freigegebene Ordner gefunden", "ok": bool(rs), "detail": ", ".join(rs) or ROOTS_SPEC})
    for r in rs:
        t = os.path.join(r, f".fpkg-webui-test-{uuid.uuid4().hex[:6]}")
        try:
            with open(t, "w") as f:
                f.write("x")
            os.remove(t)
            ok, det = True, f"frei {human(disk(r)['free'])}"
        except OSError as e:
            ok, det = False, str(e)
        checks.append({"name": f"Schreibtest {r}", "ok": ok, "detail": det})
    for label, pth in (("Ausgabeordner", DEF_OUT), ("Arbeitsordner", DEF_WORK)):
        ok = os.path.isdir(pth) and root_of(os.path.realpath(pth)) is not None
        checks.append({"name": f"{label} {pth} gemappt", "ok": ok,
                       "detail": "" if ok else "Pfad fehlt oder liegt nicht in BROWSE_ROOTS"})
    fw = disk(DEF_WORK)["free"]
    checks.append({"name": "Platz im Arbeitsordner ≥ 100 GB", "ok": bool(fw and fw >= 100 * 1024 ** 3),
                   "detail": f"frei {human(fw)}"})
    LOG.info("selftest: %d/%d ok", sum(c["ok"] for c in checks), len(checks))
    return checks


def tail_file(path, lines):
    if not os.path.exists(path):
        return ""
    with open(path, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - 400_000))
        data = f.read().decode("utf-8", "replace")
    return "\n".join(data.splitlines()[-lines:])


def bundle():
    mem = io.BytesIO()
    with zipfile.ZipFile(mem, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("diagnose.json", json.dumps({"diag": diag(), "selftest": selftest()}, indent=2, ensure_ascii=False))
        for f in sorted(glob.glob(os.path.join(DATA, "logs", "server.log*"))):
            z.write(f, "logs/" + os.path.basename(f))
        jf = os.path.join(DATA, "jobs.json")
        if os.path.exists(jf):
            z.write(jf, "jobs.json")
        with LOCK:
            last = ORDER[-15:]
        for i in last:
            lp = os.path.join(DATA, "logs", "jobs", i + ".log")
            if os.path.exists(lp):
                z.write(lp, "jobs/" + i + ".log")
    return mem.getvalue()


# ---------------------------------------------------------------- http api
def probe(path, fixes=False):
    rp = safe_path(path)
    kind, real = classify(rp)
    info = {"kind": kind, "path": real, "size": dir_size(real)}
    texts = [os.path.basename(path.rstrip("/")), real]  # where a title ID may show up
    if kind == "archive":
        vols = archive_volumes(real)
        info["size"] = sum(os.path.getsize(v) for v in vols)
        info["volumes"] = len(vols)
        state, listing = archive_encryption(real)
        info["encrypted"] = state in ("header", "data")
        texts.append(listing)
    if kind == "exfat":
        try:
            img = ExfatImage(real)
            f = img.find_app_dir()
            if f:
                info["app"] = "/" + f[0]
                info["content"] = img.size_of(f[1])
            img.close()
        except Exception as e:
            info["note"] = str(e)
    if kind in ("folder", "exfat", "ffpfsc", "ffpkg"):
        rc, out = cmd_out([CLI, "inspect", real], 120)
        m = re.search(r"param\.json: (.+)", out)
        info["param"] = m.group(1).strip() if m else None
        texts.append(out)
    if kind == "pkg":
        rc, out = cmd_out([CLI, "pkg-info", real], 120)
        bits = [re.search(rx, out) for rx in (r"Title:\s*(.+)", r"contentVersion:\s*(\S+)", r"Content ID:\s*(\S+)")]
        info["param"] = " · ".join(b.group(1).strip() for b in bits if b)
        texts.append(out)
    m = next((m for m in (RX_TITLE.search(t or "") for t in texts) if m), None)
    info["title_id"] = m.group(0).upper() if m else None
    if fixes:
        fixdir = SETTINGS.get("watch_fix_dir")
        try:
            fixdir = safe_path(fixdir, must_exist=False) if fixdir else None
        except JobError:
            fixdir = None
        info["fix_dir"] = fixdir
        info["fix_dir_exists"] = bool(fixdir and os.path.isdir(fixdir))
        info["fixes"] = list_fixes(fixdir, info["title_id"])[:20] if fixdir else []
    return info


def list_dir(path):
    if not path:
        items = []
        for r in roots():
            dk = disk(r)
            items.append({"name": root_label(r), "path": r, "dir": True, "size": None, "usable": True,
                          "free": dk["free"]})
        return {"path": "", "parent": None, "items": items}
    rp = safe_path(path)
    if not os.path.isdir(rp):
        rp = os.path.dirname(rp)
    items = []
    with os.scandir(rp) as it:
        for e in it:
            if e.name.startswith(".") or e.name in JUNK:
                continue
            try:
                isdir = e.is_dir()
                st = e.stat()
            except OSError:
                continue
            items.append({"name": e.name, "path": os.path.join(rp, e.name), "dir": isdir,
                          "size": None if isdir else st.st_size, "mtime": st.st_mtime,
                          "usable": isdir or lower_ext(e.name) in ARCHIVES + IMAGES + (".pkg",)})
    items.sort(key=lambda x: (not x["dir"], x["name"].lower()))
    parent = "" if rp in roots() else os.path.dirname(rp)
    return {"path": rp, "parent": parent, "items": items, "root": root_of(rp)}


class H(BaseHTTPRequestHandler):
    server_version = "fpkg-webui/" + APP_VERSION

    def log_message(self, fmt, *a):
        LOG.debug("http %s - %s", self.address_string(), fmt % a)

    def send(self, code, body, ctype="application/json", filename=None):
        if not isinstance(body, (bytes, bytearray)):
            body = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if filename:
            self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quote(filename)}")
        self.end_headers()
        self.wfile.write(body)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def handle_err(self, e):
        if isinstance(e, JobError):
            return self.send(400, {"error": str(e)})
        LOG.exception("http %s %s failed", self.command, self.path)
        return self.send(500, {"error": f"{type(e).__name__}: {e}"})

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if u.path in ("/", "/index.html"):
                return self.send(200, open(os.path.join(HERE, "index.html"), "rb").read(), "text/html; charset=utf-8")
            if u.path == "/api/config":
                return self.send(200, {"version": APP_VERSION, "out": DEF_OUT, "work": DEF_WORK,
                                       "roots": [{"path": r, "label": root_label(r)} for r in roots()]})
            if u.path == "/api/ls":
                return self.send(200, list_dir(q.get("path", "")))
            if u.path == "/api/probe":
                return self.send(200, probe(q.get("path", ""), fixes=q.get("fixes") == "1"))
            if u.path == "/api/free":
                return self.send(200, disk(safe_path(q.get("path", ""), must_exist=False)))
            if u.path == "/api/jobs":
                with LOCK:
                    return self.send(200, [JOBS[i].public() for i in reversed(ORDER[-60:])])
            if u.path == "/api/diag":
                return self.send(200, diag())
            if u.path == "/api/settings":
                return self.send(200, public_settings())
            if u.path == "/api/packages":
                return self.send(200, list_packages(q.get("dir") or DEF_OUT))
            if u.path == "/api/packages/info":
                return self.send(200, pkg_info(safe_path(q.get("path", ""))))
            if u.path == "/api/checks":
                with CHECK_CV:
                    return self.send(200, [public_check(CHECKS[i]) for i in reversed(CHECK_ORDER) if i in CHECKS])
            if u.path == "/api/watch":
                with WATCH_LOCK:
                    items = [{"key": k, "name": v["name"], "state": v["state"], "job": v.get("job")}
                             for k, v in WATCH["items"].items()]
                return self.send(200, {"enabled": SETTINGS["watch_enabled"], "dir": SETTINGS["watch_dir"],
                                       "fix_dir": SETTINGS["watch_fix_dir"], "last_scan": WATCH["last_scan"],
                                       "error": WATCH["error"], "items": items})
            if u.path == "/api/diag/selftest":
                return self.send(200, selftest())
            if u.path == "/api/diag/log":
                return self.send(200, {"text": tail_file(os.path.join(DATA, "logs", "server.log"),
                                                          int(q.get("lines", 400)))})
            if u.path == "/api/diag/bundle":
                return self.send(200, bundle(), "application/zip",
                                 f"fpkg-webui-diagnose-{time.strftime('%Y%m%d-%H%M%S')}.zip")
            m = re.match(r"^/api/jobs/([\w-]+)/(log|download)$", u.path)
            if m and m.group(1) in JOBS:
                j = JOBS[m.group(1)]
                if m.group(2) == "download":
                    data = open(j.logpath, "rb").read() if os.path.exists(j.logpath) else b""
                    return self.send(200, data, "text/plain; charset=utf-8", f"fpkg-job-{j.id}.log")
                off = int(q.get("offset", -1))
                data = b""
                if os.path.exists(j.logpath):
                    with open(j.logpath, "rb") as f:
                        if off < 0:
                            f.seek(0, 2)
                            off = max(0, f.tell() - 400_000)
                        f.seek(off)
                        data = f.read(2_000_000)
                return self.send(200, {"job": j.public(), "offset": off + len(data),
                                       "text": data.decode("utf-8", "replace")})
            return self.send(404, {"error": "not found"})
        except Exception as e:
            return self.handle_err(e)

    def do_POST(self):
        u = urlparse(self.path)
        try:
            if u.path == "/api/jobs":
                b = self.body()
                params = {
                    "source": safe_path((b.get("source") or "").strip()),
                    "fix": safe_path(b["fix"].strip()) if (b.get("fix") or "").strip() else None,
                    "out": safe_path((b.get("out") or "").strip() or DEF_OUT, must_exist=False),
                    "work": safe_path((b.get("work") or "").strip() or DEF_WORK, must_exist=False),
                    "preset": b.get("preset") if b.get("preset") in ("fast", "standard", "smallest") else "standard",
                    "keep_ampr": bool(b.get("keep_ampr")),
                    "keep_dump": bool(b.get("keep_dump")),
                    "confirm": bool(b.get("confirm")),
                    "cleanup": bool(b.get("cleanup")),
                    "delete_archive": bool(b.get("delete_archive")),
                    "checksum": bool(b.get("checksum", True)),
                    "full_verify": bool(b.get("full_verify")),
                    "password": (b.get("password") or "")[:500],
                    "origin": "manual",
                }
                if params["delete_archive"] and classify(params["source"])[0] != "archive":
                    params["delete_archive"] = False
                return self.send(200, enqueue(params).public())
            m = re.match(r"^/api/jobs/([\w-]+)/retry$", u.path)
            if m and m.group(1) in JOBS:
                old = JOBS[m.group(1)].params
                params = {k: v for k, v in old.items() if k not in ("watch_items",)}
                params.update(delete_archive=False, origin="manual")
                if params.get("password") == "••••":
                    params["password"] = ""
                return self.send(200, enqueue(params).public())
            m = re.match(r"^/api/jobs/([\w-]+)/(continue|cancel|cleanup|delete)$", u.path)
            if m and m.group(1) in JOBS:
                j, act = JOBS[m.group(1)], m.group(2)
                active = j.status in ("queued", "running", "waiting", "ready")
                if act == "continue" and j.status == "waiting":
                    j.status, j.step = "ready", "bereit zum Bauen"
                    j.log("== Freigegeben, wartet auf freien Build-Platz.")
                    LOG.info("job %s: released by user", j.id)
                    with QUEUE_CV:
                        QUEUE_CV.notify_all()
                elif act == "cancel" and active:
                    if j.status == "queued":
                        j.status, j.step, j.finished = "cancelled", "abgebrochen", time.time()
                    elif j.status in ("waiting", "ready"):
                        finish(j, Cancelled())
                    else:
                        threading.Thread(target=j.cancel, daemon=True).start()
                elif act == "cleanup" and not active:
                    shutil.rmtree(j.work, ignore_errors=True)
                    j.log(f"\nArbeitsordner gelöscht: {j.work}")
                    LOG.info("job %s: work dir removed by user", j.id)
                elif act == "delete" and not active:
                    with LOCK:
                        ORDER.remove(j.id)
                        JOBS.pop(j.id)
                    try:
                        os.remove(j.logpath)
                    except OSError:
                        pass
                save_jobs()
                return self.send(200, {"ok": True})
            if u.path == "/api/settings":
                update_settings(self.body())
                WATCH_KICK.set()
                with QUEUE_CV:
                    QUEUE_CV.notify_all()
                return self.send(200, public_settings())
            if u.path == "/api/settings/pushover-test":
                ok, info = send_pushover("FPKG Builder – Test", "Pushover ist korrekt eingerichtet.", 0,
                                         SETTINGS.get("webui_url") or None)
                LOG.info("pushover test: %s", "ok" if ok else info)
                return self.send(200 if ok else 400, {"ok": ok, "info": info} if ok else {"error": info})
            if u.path == "/api/checks":
                b = self.body()
                mode = "full" if b.get("mode") == "full" else "quick"
                return self.send(200, public_check(enqueue_check((b.get("path") or "").strip(), mode)))
            m = re.match(r"^/api/checks/(\w+)/cancel$", u.path)
            if m and m.group(1) in CHECKS:
                c = CHECKS[m.group(1)]
                if c["status"] == "queued":
                    c.update(status="cancelled", summary="abgebrochen", finished=time.time())
                elif c["status"] == "running" and c.get("_proc"):
                    c["cancelled"] = True
                    try:
                        os.killpg(c["_proc"].pid, signal.SIGTERM)
                    except OSError:
                        pass
                LOG.info("check %s cancel", c["id"])
                return self.send(200, {"ok": True})
            if u.path == "/api/jobs/clear-history":
                with LOCK:
                    gone = [i for i in ORDER if JOBS[i].status in ("done", "failed", "cancelled")]
                    for i in gone:
                        ORDER.remove(i)
                        JOBS.pop(i)
                save_jobs()
                LOG.info("history cleared: %d jobs", len(gone))
                return self.send(200, {"removed": len(gone)})
            if u.path == "/api/watch/scan":
                WATCH_KICK.set()
                return self.send(200, {"ok": True})
            if u.path == "/api/watch/forget":
                key = (self.body().get("key") or "")
                with WATCH_LOCK:
                    WATCH_PROCESSED.pop(key, None)
                    WATCH["items"].pop(key, None)
                save_watch()
                WATCH_KICK.set()
                LOG.info("watch: forgot %s", key)
                return self.send(200, {"ok": True})
            return self.send(404, {"error": "not found"})
        except Exception as e:
            return self.handle_err(e)


def main():
    LOG.info("FPKG WebUI %s starting on :%d", APP_VERSION, PORT)
    LOG.info("roots: %s | out=%s work=%s data=%s", roots() or "(keine!)", DEF_OUT, DEF_WORK, DATA)
    if not roots():
        LOG.warning("Keine freigegebenen Ordner gefunden (BROWSE_ROOTS=%s) – Pfade im Template prüfen", ROOTS_SPEC)
    rc, info = fpkg_info()
    LOG.info("fpkg-cli: %s", "ok" if rc == 0 else f"FEHLER rc={rc}: {info[:300]}")
    load_settings()
    load_watch()
    load_checks()
    threading.Thread(target=check_worker, daemon=True, name="checks").start()
    load_jobs()
    threading.Thread(target=prep_worker, daemon=True, name="prep").start()
    threading.Thread(target=build_worker, daemon=True, name="build").start()
    threading.Thread(target=watcher, daemon=True, name="watch").start()
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()


if __name__ == "__main__":
    main()
