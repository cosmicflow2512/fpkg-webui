"""Minimal read-only exFAT image reader (no mount, no root).

Supports bare volumes and images with an MBR or GPT partition table.
Used to copy an app folder out of a PS5 .exfat dump so a fix can be laid over it.
"""
import os
import struct

SECTOR = 512
CHUNK = 8 * 1024 * 1024


class ExfatError(Exception):
    pass


def _is_exfat(f, off):
    f.seek(off + 3)
    return f.read(8) == b"EXFAT   "


def find_volume_offset(f):
    if _is_exfat(f, 0):
        return 0
    f.seek(0)
    mbr = f.read(SECTOR)
    cands = []
    if len(mbr) == SECTOR and mbr[510:512] == b"\x55\xaa":
        for i in range(4):
            e = mbr[446 + 16 * i: 462 + 16 * i]
            ptype = e[4]
            lba = struct.unpack_from("<I", e, 8)[0]
            if ptype == 0xEE:
                f.seek(SECTOR)
                gpt = f.read(SECTOR)
                if gpt[:8] == b"EFI PART":
                    ent_lba, n, size = struct.unpack_from("<QII", gpt, 72)
                    f.seek(ent_lba * SECTOR)
                    tbl = f.read(n * size)
                    for j in range(n):
                        first = struct.unpack_from("<Q", tbl, j * size + 32)[0]
                        if first:
                            cands.append(first * SECTOR)
            elif ptype and lba:
                cands.append(lba * SECTOR)
    cands += [63 * SECTOR, 2048 * SECTOR, 128 * SECTOR]
    for off in cands:
        if _is_exfat(f, off):
            return off
    raise ExfatError("kein exFAT-Dateisystem im Image gefunden")


class Entry:
    __slots__ = ("name", "is_dir", "first", "length", "valid", "contig", "children")

    def __init__(self, name, is_dir, first, length, valid, contig):
        self.name, self.is_dir, self.first = name, is_dir, first
        self.length, self.valid, self.contig = length, valid, contig
        self.children = None


class ExfatImage:
    def __init__(self, path):
        self.path = path
        self.f = open(path, "rb", buffering=0)
        self.base = find_volume_offset(self.f)
        self.f.seek(self.base)
        bs = self.f.read(512)
        fat_off, fat_len, heap_off, self.cluster_count, root = struct.unpack_from("<IIIII", bs, 80)
        bps = 1 << bs[108]
        self.csize = bps << bs[109]
        self.fat_pos = self.base + fat_off * bps
        self.heap_pos = self.base + heap_off * bps
        self._fat = None
        self._fat_len = fat_len * bps
        self.root = Entry("", True, root, None, None, False)

    def close(self):
        self.f.close()

    # ---- low level
    def _fat_table(self):
        if self._fat is None:
            self.f.seek(self.fat_pos)
            self._fat = self.f.read(self._fat_len)
        return self._fat

    def _chain(self, first, length, contig):
        """Return list of (byte_offset, byte_len) runs for a cluster chain."""
        if first < 2:
            return []
        if contig:
            n = -(-length // self.csize)
            return [(self._cpos(first), n * self.csize)]
        fat = self._fat_table()
        runs, c, seen = [], first, 0
        limit = self.cluster_count + 2
        while 2 <= c < limit:
            pos = self._cpos(c)
            if runs and runs[-1][0] + runs[-1][1] == pos:
                runs[-1] = (runs[-1][0], runs[-1][1] + self.csize)
            else:
                runs.append((pos, self.csize))
            seen += self.csize
            if length is not None and seen >= length:
                break
            c = struct.unpack_from("<I", fat, c * 4)[0]
        return runs

    def _cpos(self, c):
        return self.heap_pos + (c - 2) * self.csize

    def _read_runs(self, runs, length):
        out, left = bytearray(), length
        for pos, ln in runs:
            take = ln if left is None else min(ln, left)
            self.f.seek(pos)
            out += self.f.read(take)
            if left is not None:
                left -= take
                if left <= 0:
                    break
        return bytes(out)

    # ---- directories
    def listdir(self, d):
        if d.children is not None:
            return d.children
        raw = self._read_runs(self._chain(d.first, d.length, d.contig), d.length)
        kids, i = [], 0
        while i + 32 <= len(raw):
            t = raw[i]
            if t == 0x00:
                break
            if t != 0x85:
                i += 32
                continue
            sec = raw[i + 1]
            attrs = struct.unpack_from("<H", raw, i + 4)[0]
            st = raw[i + 32: i + 64]
            if len(st) < 32 or st[0] != 0xC0:
                i += 32
                continue
            flags, nlen = st[1], st[3]
            valid, = struct.unpack_from("<Q", st, 8)
            first, length = struct.unpack_from("<IQ", st, 20)
            name = b""
            for k in range(2, sec + 1):
                ne = raw[i + 32 * k: i + 32 * k + 32]
                if len(ne) == 32 and ne[0] == 0xC1:
                    name += ne[2:32]
            name = name.decode("utf-16-le", "replace")[:nlen]
            kids.append(Entry(name, bool(attrs & 0x10), first, length, valid, bool(flags & 0x02)))
            i += 32 * (sec + 1)
        d.children = kids
        return kids

    def walk(self, d=None, prefix=""):
        d = d or self.root
        for e in self.listdir(d):
            p = f"{prefix}/{e.name}" if prefix else e.name
            yield p, e
            if e.is_dir:
                yield from self.walk(e, p)

    def find_app_dir(self, depth=3):
        """Path (relative) of the first folder holding sce_sys/ and eboot.bin."""
        def rec(d, prefix, lvl):
            names = {e.name: e for e in self.listdir(d)}
            if "sce_sys" in names and names["sce_sys"].is_dir and "eboot.bin" in names:
                return prefix, d
            if lvl >= depth:
                return None
            for e in names.values():
                if e.is_dir:
                    r = rec(e, f"{prefix}/{e.name}" if prefix else e.name, lvl + 1)
                    if r:
                        return r
            return None
        return rec(self.root, "", 0)

    def size_of(self, d):
        return sum(e.length for _, e in self.walk(d) if not e.is_dir)

    # ---- extraction
    def extract(self, d, dest, progress=None):
        total = self.size_of(d)
        done = [0]
        os.makedirs(dest, exist_ok=True)

        def rec(node, out):
            for e in self.listdir(node):
                target = os.path.join(out, e.name)
                if e.is_dir:
                    os.makedirs(target, exist_ok=True)
                    rec(e, target)
                    continue
                with open(target, "wb") as w:
                    left = e.valid
                    for pos, ln in self._chain(e.first, e.length, e.contig):
                        if left <= 0:
                            break
                        take = min(ln, left)
                        self.f.seek(pos)
                        while take > 0:
                            buf = self.f.read(min(CHUNK, take))
                            if not buf:
                                raise ExfatError(f"Image endet unerwartet bei {target}")
                            w.write(buf)
                            take -= len(buf)
                            left -= len(buf)
                            done[0] += len(buf)
                            if progress:
                                progress(done[0], total)
                    if e.length > e.valid:
                        w.truncate(e.length)
                        done[0] += e.length - e.valid
        rec(d, dest)
        return total


if __name__ == "__main__":
    import sys
    img = ExfatImage(sys.argv[1])
    found = img.find_app_dir()
    print("volume offset", img.base, "cluster", img.csize, "app dir", found[0] if found else None)
    if len(sys.argv) > 2 and found:
        img.extract(found[1], sys.argv[2])
        print("extracted")
