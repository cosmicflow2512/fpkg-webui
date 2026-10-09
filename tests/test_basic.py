"""Basic tests without fpkg-cli: archive volumes, classification, progress parsing, exFAT reader.

Run: python tests/test_basic.py   (the exFAT content test needs root + loop mount, it is skipped otherwise)
"""
import hashlib
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app"))
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp())
import server  # noqa: E402
from exfat import ExfatImage  # noqa: E402

FAILS = []


def check(name, cond):
    print(("ok   " if cond else "FAIL ") + name)
    if not cond:
        FAILS.append(name)


def touch(d, *names):
    for n in names:
        open(os.path.join(d, n), "w").close()


def test_volumes():
    d = tempfile.mkdtemp()
    touch(d, "g.7z.001", "g.7z.002", "h.part1.rar", "h.part2.rar", "h.part10.rar", "k.rar", "k.r00", "k.r01",
          "z.zip", "z.z01", "solo.7z", "base.pkg", "img.exfat")
    b = os.path.basename
    check("7z.002 -> 7z.001", b(server.classify(os.path.join(d, "g.7z.002"))[1]) == "g.7z.001")
    check("part2.rar -> part1.rar", b(server.classify(os.path.join(d, "h.part2.rar"))[1]) == "h.part1.rar")
    check("k.r00 -> k.rar", b(server.classify(os.path.join(d, "k.r00"))[1]) == "k.rar")
    check("z.z01 -> z.zip", b(server.classify(os.path.join(d, "z.z01"))[1]) == "z.zip")
    check("rar volumes", [b(v) for v in server.archive_volumes(os.path.join(d, "h.part1.rar"))]
          == ["h.part1.rar", "h.part10.rar", "h.part2.rar"])
    check("old rar volumes", len(server.archive_volumes(os.path.join(d, "k.rar"))) == 3)
    check("solo 7z", server.archive_volumes(os.path.join(d, "solo.7z")) == [os.path.join(d, "solo.7z")])
    check("pkg kind", server.classify(os.path.join(d, "base.pkg"))[0] == "pkg")
    check("exfat kind", server.classify(os.path.join(d, "img.exfat"))[0] == "exfat")


def test_old_rar_naming():
    d = tempfile.mkdtemp()
    touch(d, "d.rar", "d.r00", "d.r99", "d.s00", "d.s83", "d.sfv", "d.nfo", "z.zip", "z.z01")
    vols = [os.path.basename(v) for v in server.archive_volumes(os.path.join(d, "d.rar"))]
    check("old rar: .rar + .r00-.r99 + .s00-.s83", vols == ["d.r00", "d.r99", "d.rar", "d.s00", "d.s83"])
    check("old rar: .s83 -> .rar", os.path.basename(server.classify(os.path.join(d, "d.s83"))[1]) == "d.rar")
    check("old rar: .sfv not a volume", "d.sfv" not in vols)
    check("zip: .z01 still zip", [os.path.basename(v) for v in server.archive_volumes(os.path.join(d, "z.zip"))]
          == ["z.z01", "z.zip"])


def test_progress():
    j = server.Job.__new__(server.Job)
    j.prog = {"pct": None, "phase": "", "detail": "", "eta": ""}
    j.step_started = time.time() - 60
    j.bp = {"total": 80_000_000_000, "data": 0.0, "large": 0, "lpct": 0.0, "base": None, "bar": 0.0}
    seq = []
    for line in [
        "[░░░░░░░░░░░░░░░░░░░░░░░░]   2.5% · Kraken-compressing & writing the inner image 0% · elapsed 00:10",
        "06:17:14   [inner]   data  33% (4/43): /Runtime/chunk0.rpkg -> 19,229,280,516 bytes (Kraken, ratio 65.9 %)",
        "06:17:14   [inner]   processing large file: /Runtime/chunk1.rpkg (57,519,401,352 bytes)",
        "06:24:24   [inner]     Kraken level 4:  30% of /Runtime/chunk1.rpkg",
        "06:29:30   [inner]     Kraken level 4:  80% of /Runtime/chunk1.rpkg",
        "[█████████████████████░░░]  88.9% · Generating NAPS tables (files, blocks, integrity) 100% · elapsed 40:04 · ~02:00 left",
        "[████████████████████████] 100.0% · Done 100% · elapsed 42:04",
    ]:
        server.parse_fpkg(j, line)
        seq.append(j.prog["pct"])
    check("progress monotonic", all(b >= a for a, b in zip(seq, seq[1:])))
    check("progress interpolates large file", seq[3] < seq[4] and 30 < seq[3] < 88)
    check("progress ends at 100", seq[-1] == 100)
    check("phase parsed", j.prog["phase"] == "Done")


def test_checksums():
    d = tempfile.mkdtemp()
    touch(d, "a.7z", "b.rar", "c.pkg")
    with open(os.path.join(d, "SHA-256.txt"), "w") as f:
        f.write("ab" * 32 + "\n")
    with open(os.path.join(d, "x.sfv"), "w") as f:
        f.write("; comment\nb.rar 1a2b3c4d\n")
    with open(os.path.join(d, "SHA256SUMS"), "w") as f:
        f.write("cd" * 32 + "  *c.pkg\nSHA256 (a.7z) = " + "ef" * 32 + "\n")
    sums = server.find_checksums(os.path.join(d, "a.7z"))
    check("bare hash -> '*'", sums.get("*", (None,))[0] == "sha256")
    check("sfv entry", sums.get("b.rar", (None,))[0] == "crc32")
    check("sha256sum entry", sums.get("c.pkg", (None, ""))[1] == "cd" * 32)
    check("bsd entry", sums.get("a.7z", (None, ""))[1] == "ef" * 32)


def make_mbr_wrapped(src_img, dst):
    data = open(src_img, "rb").read()
    mbr = bytearray(512)
    mbr[510:512] = b"\x55\xaa"
    e = bytearray(16)
    e[4] = 0x07
    struct.pack_into("<II", e, 8, 2048, len(data) // 512)
    mbr[446:462] = e
    with open(dst, "wb") as f:
        f.write(bytes(mbr) + bytes(2048 * 512 - 512) + data)


def sha_tree(root):
    out = {}
    for cur, _, files in os.walk(root):
        for f in files:
            p = os.path.join(cur, f)
            out[os.path.relpath(p, root)] = hashlib.sha256(open(p, "rb").read()).hexdigest()
    return out


def test_exfat():
    if not shutil.which("mkfs.exfat"):
        print("skip exFAT tests (mkfs.exfat missing)")
        return
    d = tempfile.mkdtemp()
    img = os.path.join(d, "t.exfat")
    with open(img, "wb") as f:
        f.truncate(128 * 1024 * 1024)
    subprocess.run(["mkfs.exfat", img], check=True, stdout=subprocess.DEVNULL)
    e = ExfatImage(img)
    check("exfat empty: no app dir", e.find_app_dir() is None)
    e.close()
    if os.geteuid() != 0:
        print("skip exFAT content test (needs root for loop mount)")
        return
    mnt = os.path.join(d, "m")
    os.makedirs(mnt)
    if subprocess.run(["mount", "-o", "loop", img, mnt]).returncode != 0:
        print("skip exFAT content test (mount failed)")
        return
    try:
        app = os.path.join(mnt, "PPSA00000-app0")
        os.makedirs(os.path.join(app, "sce_sys"))
        os.makedirs(os.path.join(app, "data"))
        with open(os.path.join(app, "eboot.bin"), "wb") as f:
            f.write(os.urandom(3_000_000))
        for i in range(30):  # interleaved writes -> fragmented FAT chains
            for n in ("a.bin", "b.bin"):
                with open(os.path.join(app, "data", n), "ab") as f:
                    f.write(os.urandom(300_000))
        for i in range(200):
            with open(os.path.join(app, "data", f"datei mit langem namen {i} (äöü).txt"), "w") as f:
                f.write(str(i))
        open(os.path.join(app, "empty"), "w").close()
        ref = sha_tree(app)
    finally:
        subprocess.run(["umount", mnt])
    wrapped = os.path.join(d, "t_mbr.img")
    make_mbr_wrapped(img, wrapped)
    for path in (img, wrapped):
        e = ExfatImage(path)
        rel, node = e.find_app_dir()
        out = os.path.join(d, "out-" + os.path.basename(path))
        e.extract(node, out)
        e.close()
        check(f"exfat extract identical ({os.path.basename(path)})", sha_tree(out) == ref)


def test_nested_archives():
    """DUPLEX-style: split RAR -> one inner RAR -> app folder. 7-Zip is replaced by a fake that 'extracts' from a map."""
    base = tempfile.mkdtemp()
    os.makedirs(os.path.join(base, "logs", "jobs"), exist_ok=True)
    server.DATA = base
    contents = {}  # archive basename -> callable(dest)

    def app_into(dest):
        os.makedirs(os.path.join(dest, "PPSA00000-app0", "sce_sys"))
        touch(os.path.join(dest, "PPSA00000-app0"), "eboot.bin")

    def inner_into(name, nxt):
        def f(dest):
            os.makedirs(dest, exist_ok=True)
            touch(dest, name, "release.nfo")
            contents[name] = nxt
        return f

    def run(levels):
        j = server.Job({"work": base, "source": "x"})
        os.makedirs(os.path.join(j.work, "src"))
        j.set_plan(["Quelle entpacken", "Quelle prüfen", "FPKG bauen"])
        j.sevenz = lambda arc, dest, need_build=False: contents[os.path.basename(arc)](dest)
        nxt = app_into
        for i in range(levels, 0, -1):
            nxt = inner_into(f"inner{i}.rar", nxt)
        nxt(os.path.join(j.work, "src"))
        return j, j.unwrap_nested(os.path.join(j.work, "src"))

    j, (kind, path) = run(1)
    check("nested: inner archive unpacked to app folder", kind == "folder" and path.endswith("PPSA00000-app0"))
    check("nested: inner archive deleted", not os.path.exists(os.path.join(j.work, "src", "inner1.rar")))
    check("nested: plan step after 'Quelle entpacken'",
          [x["label"] for x in j.plan][:2] == ["Quelle entpacken", "Inneres Archiv entpacken"])
    j, (kind, path) = run(server.MAX_NEST)
    check("nested: MAX_NEST levels ok", kind == "folder")
    try:
        run(server.MAX_NEST + 1)
        check("nested: too deep raises", False)
    except server.JobError:
        check("nested: too deep raises", True)
    j, (kind, path) = run(0)
    check("nested: plain archive unchanged", kind == "folder" and "Inneres Archiv entpacken" not in [x["label"] for x in j.plan])


def test_fix_suggestions():
    root = tempfile.mkdtemp()
    fixdir = os.path.join(root, "fixes")
    os.makedirs(os.path.join(fixdir, "PPSA28416 DLC Unlocker"))
    touch(fixdir, "[DLPSGAME.COM]-FW_403_PPSA34547_CONTROL_backport_files.zip", "other.part1.rar", "other.part2.rar",
          "readme.txt", "PPSA99999 fix.7z")
    os.utime(os.path.join(fixdir, "PPSA99999 fix.7z"), (1, 1))
    fx = server.list_fixes(fixdir, "PPSA34547")
    names = [f["name"] for f in fx]
    check("fixes: matching title ID first", names[0].startswith("[DLPSGAME.COM]-FW_403_PPSA34547") and fx[0]["match"])
    check("fixes: only first volume listed", "other.part1.rar" in names and "other.part2.rar" not in names)
    check("fixes: non-archives skipped", "readme.txt" not in names)
    check("fixes: folders listed", "PPSA28416 DLC Unlocker" in names)
    check("fixes: find_fix uses match", server.find_fix(fixdir, "ppsa28416").endswith("PPSA28416 DLC Unlocker"))
    check("fixes: no match -> None", server.find_fix(fixdir, "PPSA00001") is None)
    # probe: title ID taken from the archive listing when the name has none (DUPLEX naming)
    src = os.path.join(root, "src")
    os.makedirs(src)
    touch(src, "STAR.WARS.Galactic.Racer.PS5-DUPLEX.part001.rar")
    old = server.archive_encryption, server.roots
    server.archive_encryption = lambda a: ("none", "Path = PPSA28416-app0/eboot.bin\n")
    server.roots = lambda: [root]
    server.SETTINGS["watch_fix_dir"] = fixdir
    r = server.probe(os.path.join(src, "STAR.WARS.Galactic.Racer.PS5-DUPLEX.part001.rar"), fixes=True)
    check("probe: title ID from listing", r["title_id"] == "PPSA28416")
    check("probe: matching fix suggested", r["fixes"][0]["name"] == "PPSA28416 DLC Unlocker" and r["fix_dir_exists"])
    server.archive_encryption, server.roots = old


if __name__ == "__main__":
    test_volumes()
    test_old_rar_naming()
    test_progress()
    test_checksums()
    test_nested_archives()
    test_fix_suggestions()
    test_exfat()
    print(f"\n{len(FAILS)} Fehler" if FAILS else "\nalle Tests ok")
    sys.exit(1 if FAILS else 0)
