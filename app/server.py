#!/usr/bin/env python3
"""FPKG WebUI - web front end for PSVIETHOA fpkg-cli (PS5 FPKG builder) on Unraid.

Python stdlib only. One build at a time, further jobs wait in a queue.
"""
import glob
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
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

from exfat import ExfatError, ExfatImage

APP_VERSION = "1.0.0"
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
    if (re.search(r"\.(7z|zip|rar|tar)\.\d{3}$", n) or re.search(r"\.part\d+\.rar$", n)
            or re.search(r"\.(r|z)\d{2,3}$", n)):
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
    m = re.match(r"^(.*)\.(7z|zip|rar|tar)\.\d{3}$", low)
    if m:
        pats.append(re.escape(m.group(1)) + r"\." + m.group(2) + r"\.\d{3}")
    m = re.match(r"^(.*)\.(r|z)\d{2,3}$", low)
    if m:
        low = m.group(1) + (".rar" if m.group(2) == "r" else ".zip")
    m = re.match(r"^(.*)\.part\d+\.rar$", low)
    if m:
        pats.append(re.escape(m.group(1)) + r"\.part\d+\.rar")
    elif low.endswith(".rar"):
        b = re.escape(low[:-4])
        pats += [b + r"\.rar", b + r"\.r\d{2,3}"]
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
          "finished", "started")


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
        self._init_runtime()

    def _init_runtime(self):
        self.proc = None
        self.cancel_flag = False
        self.go = threading.Event()
        self.keep_work = False
        self.step_started = time.time()
        self.logpath = os.path.join(DATA, "logs", "jobs", self.id + ".log")
        self.work = os.path.join(self.params["work"], "job-" + self.id)

    def public(self):
        return {k: getattr(self, k) for k in PUBLIC}

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
        self.log("$ " + " ".join(f'"{c}"' if " " in c else c for c in cmd))
        LOG.debug("job %s exec: %s", self.id, cmd)
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
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

    def sevenz(self, archive, dest):
        self.run([SEVENZ, "x", "-y", "-bso0", "-bse1", "-bsp1", f"-o{dest}", archive], parse_7z)
        self.set_progress(100)

    # -- pipeline
    def execute(self):
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

        in_work = False
        if kind == "archive":
            self.set_step("Quelle entpacken")
            vols = archive_volumes(src)
            self.log("Archiv-Teile: " + ", ".join(os.path.basename(v) for v in vols)
                     + f" ({human(sum(os.path.getsize(v) for v in vols))})")
            dst = os.path.join(self.work, "src")
            self.sevenz(src, dst)
            strip_junk(dst)
            inner_kind, inner = classify(dst)
            self.log(f"Im Archiv erkannt: {inner_kind} -> {inner}")
            if inner_kind == "archive":
                raise JobError("Archiv enthält nur ein weiteres Archiv – bitte das innere Archiv direkt wählen")
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

        if p.get("confirm"):
            self.set_step("Freigabe")
            self.status = "waiting"
            self.step = "Wartet auf Freigabe – Log prüfen, dann 'Bauen'"
            self.log("== Angehalten. Erkennung und Fix-Abgleich oben prüfen, dann in der Oberfläche 'Bauen' klicken.")
            save_jobs()
            while not self.go.wait(1):
                self.check()
            self.check()
            self.status = "running"
            LOG.info("job %s: released by user", self.id)

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
        rc, _ = self.capture([CLI, "verify", final])
        res["verify"] = rc == 0
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
            free = disk(self.work)["free"]
            if free is not None and free < total * 1.05:
                raise JobError(f"Zu wenig Platz: braucht ~{human(total)}, frei {human(free)}")
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
            data = [JOBS[i].public() for i in ORDER[-200:]]
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
            if j.status in ("queued", "running", "waiting"):
                j.status, j.error, j.step = "failed", "Container wurde neu gestartet", "Fehler"
            JOBS[j.id] = j
            ORDER.append(j.id)
        LOG.info("loaded %d jobs from history", len(ORDER))
    except Exception:
        LOG.exception("could not load jobs.json")


def worker():
    while True:
        try:
            worker_step()
        except Exception:
            LOG.exception("worker crashed, continuing")
            time.sleep(1)


def worker_step():
    with QUEUE_CV:
        while True:
            nxt = next((JOBS[i] for i in ORDER if JOBS[i].status == "queued"), None)
            if nxt:
                break
            QUEUE_CV.wait()
        nxt.status = "running"
    LOG.info("job %s: start source=%s fix=%s", nxt.id, nxt.params["source"], nxt.params.get("fix"))
    save_jobs()
    try:
        nxt.execute()
        nxt.status, nxt.step = "done", "fertig"
        LOG.info("job %s: done -> %s", nxt.id, (nxt.result or {}).get("pkg"))
    except Cancelled:
        nxt.status, nxt.step = "cancelled", "abgebrochen"
        nxt.log("\n== abgebrochen")
        LOG.info("job %s: cancelled", nxt.id)
        cleanup_after_fail(nxt)
    except Exception as e:
        nxt.status, nxt.error, nxt.step = "failed", str(e), "Fehler"
        nxt.log("\nFEHLER: " + str(e))
        if not isinstance(e, JobError):
            nxt.log(traceback.format_exc())
            LOG.exception("job %s: unexpected error", nxt.id)
        else:
            LOG.error("job %s: %s", nxt.id, e)
        cleanup_after_fail(nxt)
    for x in nxt.plan:
        if x["state"] == "active":
            x["state"] = "failed" if nxt.status != "done" else "done"
    nxt.finished = time.time()
    save_jobs()


def cleanup_after_fail(j):
    if j.keep_work:
        j.log(f"Arbeitsordner bleibt erhalten (Quellarchiv wurde gelöscht): {j.work}")
        chown_tree(j.work)
    elif j.params.get("cleanup"):
        shutil.rmtree(j.work, ignore_errors=True)
        j.log(f"Arbeitsordner gelöscht: {j.work}")


# ---------------------------------------------------------------- diagnostics
_INFO_CACHE = {"t": 0, "v": None}


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
def probe(path):
    rp = safe_path(path)
    kind, real = classify(rp)
    info = {"kind": kind, "path": real, "size": dir_size(real)}
    if kind == "archive":
        vols = archive_volumes(real)
        info["size"] = sum(os.path.getsize(v) for v in vols)
        info["volumes"] = len(vols)
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
    if kind == "pkg":
        rc, out = cmd_out([CLI, "pkg-info", real], 120)
        bits = [re.search(rx, out) for rx in (r"Title:\s*(.+)", r"contentVersion:\s*(\S+)", r"Content ID:\s*(\S+)")]
        info["param"] = " · ".join(b.group(1).strip() for b in bits if b)
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
                return self.send(200, probe(q.get("path", "")))
            if u.path == "/api/free":
                return self.send(200, disk(safe_path(q.get("path", ""), must_exist=False)))
            if u.path == "/api/jobs":
                with LOCK:
                    return self.send(200, [JOBS[i].public() for i in reversed(ORDER[-60:])])
            if u.path == "/api/diag":
                return self.send(200, diag())
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
                }
                if params["delete_archive"] and classify(params["source"])[0] != "archive":
                    params["delete_archive"] = False
                os.makedirs(params["out"], exist_ok=True)
                os.makedirs(params["work"], exist_ok=True)
                j = Job(params)
                with QUEUE_CV:
                    JOBS[j.id] = j
                    ORDER.append(j.id)
                    QUEUE_CV.notify_all()
                LOG.info("job %s: queued %s", j.id, json.dumps(params, ensure_ascii=False))
                save_jobs()
                return self.send(200, j.public())
            m = re.match(r"^/api/jobs/([\w-]+)/(continue|cancel|cleanup|delete)$", u.path)
            if m and m.group(1) in JOBS:
                j, act = JOBS[m.group(1)], m.group(2)
                active = j.status in ("queued", "running", "waiting")
                if act == "continue" and j.status == "waiting":
                    j.go.set()
                elif act == "cancel" and active:
                    if j.status == "queued":
                        j.status, j.step = "cancelled", "abgebrochen"
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
    load_jobs()
    threading.Thread(target=worker, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()


if __name__ == "__main__":
    main()
