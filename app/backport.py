"""Backport -> PS5 (ShadowMountPlus): helpers without job state.

ShadowMountPlus overlays <scanpath>/backports/<TITLE_ID>/ (and the fallback /data/homebrew/backports/<TITLE_ID>/)
on the matching game right before the game process starts, installed PKGs included (1.7beta2+).
Source: https://github.com/drakmor/ShadowMountPlus (README "Backport overlay behavior", docs/api.md).
"""
import ftplib
import json
import os
import posixpath
import re
import time
import urllib.error
import urllib.request

RX_TITLE = re.compile(r"(PPSA|PPSB|CUSA)\d{5}", re.I)
# DLPSGAME style "..._04_000_000_backport..." -> 04.000.000
RX_VER = re.compile(r"(?<![\d.])(\d{2})[_.](\d{3})[_.](\d{3})(?![\d])")
RX_VER2 = re.compile(r"(?:^|[\s_\-\[(])v(\d{1,2}\.\d{2,3}(?:\.\d{3})?)(?![\d])", re.I)
RX_FW = re.compile(r"FW[\s_\-]?(\d{1,2})(?:[._]?(?:xx|x{1,2}|\d{2}))", re.I)
MARK_DIRS = {"sce_sys", "sce_module", "fakelib", "fakelib2"}
MARK_FILES = {"eboot.bin"}
JUNK = {".DS_Store", "Thumbs.db", "desktop.ini", "__MACOSX", ".fseventsd", ".Spotlight-V100", ".Trashes"}
# removed by ShadowMountPlus from backports/<ID>/fakelib before mounting it for an installed PPR PKG (README)
SMP_DROPS = ("libSceAmpr.sprx", "libScePlayGo.sprx", "libkernel.sprx")
SMP_REDIRECT_LIMIT = 256


def is_junk(rel):
    parts = rel.split("/")
    return any(p in JUNK for p in parts) or parts[-1].startswith("._")


def name_info(name):
    """Title ID, game version and firmware target from a backport file/folder name."""
    out = {"title_id": None, "version": None, "fw": None}
    m = RX_TITLE.search(name)
    if m:
        out["title_id"] = m.group(0).upper()
    m = RX_VER.search(name)
    if m:
        out["version"] = ".".join(m.groups())
    else:
        m = RX_VER2.search(name)
        if m:
            out["version"] = m.group(1)
    m = RX_FW.search(name)
    if m:
        out["fw"] = f"{int(m.group(1))}.xx"
    return out


def find_roots(files):
    """files: relative paths (posix) of all files in the backport.
    -> list of root prefixes ('' = top level): the directory holding eboot.bin / sce_sys / sce_module / fakelib.
    More than one root = several variants (e.g. one folder per firmware)."""
    roots = set()
    for rel in files:
        if is_junk(rel):
            continue
        parts = rel.split("/")
        for i, p in enumerate(parts):
            last = i == len(parts) - 1
            if (last and p.lower() in MARK_FILES) or (not last and p.lower() in MARK_DIRS):
                roots.add("/".join(parts[:i]))
                break
    if not roots:
        # no marker: unwrap single top-level folders
        prefix = ""
        rest = [f for f in files if not is_junk(f)]
        while rest and all("/" in f for f in rest):
            tops = {f.split("/", 1)[0] for f in rest}
            if len(tops) != 1:
                break
            top = tops.pop()
            prefix = prefix + top + "/"
            rest = [f.split("/", 1)[1] for f in rest]
        return [prefix.rstrip("/")]
    # drop roots nested inside another root (e.g. a sce_sys copy inside fakelib)
    res = sorted(roots, key=len)
    keep = []
    for r in res:
        if not any(k == "" or r == k or r.startswith(k + "/") for k in keep):
            keep.append(r)
    return sorted(keep)


def files_under(files, root):
    pre = root + "/" if root else ""
    return [f[len(pre):] for f in files if f.startswith(pre) and not is_junk(f)]


def check_files(rel):
    """Warnings/notes for the files that will be uploaded (paths relative to the backport root)."""
    notes = []
    if len(rel) > SMP_REDIRECT_LIMIT:
        notes.append(f"{len(rel)} Dateien – ShadowMountPlus unterstützt bei installierten PKGs bis zu "
                     f"{SMP_REDIRECT_LIMIT} Redirect-Einträge pro Spiel (ob Ordner mitzählen: ungeprüft)")
    drops = [f for f in rel if f.split("/")[0] == "fakelib" and f.split("/")[-1] in SMP_DROPS]
    if drops:
        notes.append("ShadowMountPlus löscht bei installierten PKGs vor dem Start aus fakelib: "
                     + ", ".join(sorted(os.path.basename(d) for d in drops)))
    if not any(f.split("/")[0].lower() in ("fakelib", "fakelib2", "sce_module") or f.lower() == "eboot.bin"
               for f in rel):
        notes.append("Weder eboot.bin noch sce_module/ oder fakelib/ gefunden – ist das wirklich ein Backport?")
    return notes


def version_tuple(v):
    nums = [int(x) for x in re.findall(r"\d+", v or "")]
    return tuple(nums) if nums else None


def same_version(a, b):
    ta, tb = version_tuple(a), version_tuple(b)
    if ta is None or tb is None:
        return None
    n = max(len(ta), len(tb))
    return ta + (0,) * (n - len(ta)) == tb + (0,) * (n - len(tb))


def slt_entries(listing):
    """Entries of `7z l -slt`: [(path, size, is_dir)] (archive header block skipped)."""
    if "\n----------" in listing:
        listing = listing.split("\n----------", 1)[1]
    out, cur = [], {}
    for ln in listing.splitlines() + [""]:
        if not ln.strip():
            if cur.get("Path"):
                out.append((cur["Path"].replace("\\", "/"), int(cur.get("Size") or 0),
                            cur.get("Folder") == "+" or "D" in (cur.get("Attributes") or "").split(" ")[0]))
            cur = {}
            continue
        if " = " in ln:
            k, v = ln.split(" = ", 1)
            cur[k.strip()] = v.strip()
    return out


# ---------------------------------------------------------------- ShadowMountPlus API
def smp_call(host, port, route, body=None, timeout=8):
    """POST JSON to the ShadowMountPlus API (needs api_bind_address=0.0.0.0 on the PS5)."""
    req = urllib.request.Request(f"http://{host}:{port}{route}", data=json.dumps(body or {}).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8", "replace") or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8", "replace") or "{}")
        except ValueError:
            return e.code, {"error": f"HTTP {e.code}"}


def smp_game(host, port, title_id):
    """-> game dict from /api/v1/games (includes installed PKGs) or None."""
    code, data = smp_call(host, port, "/api/v1/games", {"include_size": False}, timeout=20)
    if code != 200:
        raise OSError(f"ShadowMountPlus-API /api/v1/games: HTTP {code} {data.get('error', '')}".strip())
    for g in data.get("games") or []:
        if (g.get("title_id") or "").upper() == title_id.upper():
            return g
    return None


# ---------------------------------------------------------------- FTP
class Ftp:
    def __init__(self, host, port, user="", password="", timeout=30):
        self.f = ftplib.FTP()
        self.f.connect(host, int(port), timeout=timeout)
        self.f.login(user or "anonymous", password or "anonymous@")
        try:
            self.f.voidcmd("TYPE I")
        except ftplib.all_errors:
            pass
        # ps5-payload-dev/ftpsrv converts SELF->ELF on the fly per connection (default on): SIZE/MLSD then report the
        # decrypted size of eboot.bin/*.sprx. "SITE SELF" toggles it; other servers reject it, which is fine.
        self.self_mode = None
        try:
            r = self.f.sendcmd("SITE SELF")
            if "enabled" in r.lower():
                r = self.f.sendcmd("SITE SELF")
            self.self_mode = "disabled" if "disabled" in r.lower() else None
        except ftplib.all_errors:
            pass

    def close(self):
        try:
            self.f.quit()
        except Exception:
            try:
                self.f.close()
            except Exception:
                pass

    def isdir(self, path):
        cur = self.f.pwd()
        try:
            self.f.cwd(path)
            return True
        except ftplib.error_perm:
            return False
        finally:
            try:
                self.f.cwd(cur)
            except ftplib.all_errors:
                pass

    def makedirs(self, path):
        parts = [p for p in path.split("/") if p]
        cur = ""
        for p in parts:
            cur += "/" + p
            if not self.isdir(cur):
                self.f.mkd(cur)

    def listdir(self, path):
        """-> [(name, is_dir)]. CWD first: ftpsrv's MLSD/LIST ignore the path argument and list the cwd,
        and it has no OPTS/NLST."""
        cur = self.f.pwd()
        self.f.cwd(path)
        try:
            lines = []
            try:
                self.f.retrlines("MLSD", lines.append)
                out = []
                for ln in lines:
                    facts, _, name = ln.partition(" ")
                    f = {k.lower(): v for k, _, v in (x.partition("=") for x in facts.split(";") if x)}
                    if name and name not in (".", "..") and f.get("type") in ("dir", "file"):
                        out.append((name, f["type"] == "dir"))
                return out
            except ftplib.error_perm:
                lines = []
                self.f.retrlines("LIST", lines.append)
                out = []
                for ln in lines:
                    parts = ln.split(None, 8)
                    if len(parts) == 9 and parts[8] not in (".", ".."):
                        out.append((parts[8], ln.startswith("d")))
                return out
        finally:
            try:
                self.f.cwd(cur)
            except ftplib.all_errors:
                pass

    def rmtree(self, path):
        for n, d in self.listdir(path):
            p = posixpath.join(path, n)
            if d:
                self.rmtree(p)
            else:
                self.f.voidcmd("DELE " + p)  # ftpsrv answers 226, ftplib.delete() only accepts 250/200
        self.f.rmd(path)

    def rename(self, a, b):
        self.f.rename(a, b)

    def size(self, path):
        try:
            return self.f.size(path)
        except ftplib.all_errors:
            return None

    def upload(self, local, remote, cb=None):
        with open(local, "rb") as fh:
            self.f.storbinary(f"STOR {remote}", fh, blocksize=1024 * 1024, callback=cb)


def backup_name(title_id, tag=""):
    return f"{title_id}-{time.strftime('%Y%m%d-%H%M%S')}" + (f"-{tag}" if tag else "")
