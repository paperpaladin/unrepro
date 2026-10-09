#!/usr/bin/env python3
"""unrepro -- build a project twice under deliberately VARIED conditions, then
explain why the outputs differ.

Two back-to-back builds in the same directory, same clock, same environment will
usually match even when the build is hopelessly unreproducible, so a "clean"
result from two identical builds proves nothing at all. The whole value is in
what we change between build 1 and build 2: the build directory path, the clock
and timezone, the locale, the umask, the apparent hostname and user, and the
make parallelism. Each of those is a real, documented source of irreproducible
output in the wild, and each one is invisible unless you vary it.

Varying the build PATH matters as much as varying the clock. The clock class of
bug (`__DATE__`, `date >> version.h`, archive member mtimes) is the famous one,
but path leakage is at least as common and much harder to notice: anything that
compiles with an absolute source path, or bakes `__FILE__`, `getcwd()`, or a
`-I` prefix into an output, ships the identity of one particular machine's
directory layout inside the artifact. Two builds in the same directory can never
show you that.

When the outputs differ, "files differ" is not a finding. This tool names the
mechanism: which byte ranges, which ELF section, which `ar` header field, which
gzip header, and whether the differing bytes decode as a plausible date or
contain a build path we know we planted.

Python 3 stdlib only. No diffoscope, no network.
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time

# 2009-02-13T23:31:30Z. A fixed, obviously-not-now value, so that when it shows
# up in an output we can say "that is the SOURCE_DATE_EPOCH we handed build B"
# rather than merely "that looks like a date".
EPOCH_B = 1234567890

# 1990-01-01 .. 2040-01-01. Anything outside this is probably not a timestamp,
# it is just a number that happens to be 4 bytes wide.
PLAUSIBLE = (631152000, 2208988800)

# Bytes that can plausibly be text. A candidate timestamp made entirely of them
# is almost certainly a string: b"Beta" unpacks to 1635018050 = 2021-10-23, and
# b"n\nGa" to 1194291310 = 2007-11-05.
TEXTISH = frozenset(b"\t\n\r") | frozenset(range(0x20, 0x7f))

# shortcut: a flat size threshold for the full byte-offset diff. A 4KiB file
# gets every differing range dumped; a 4MiB one gets none, even when the single
# differing byte would have been the whole answer. Upgrade: always dump, but cap
# the number of ranges rather than the file size.
HEX_LIMIT = 4096      # full byte-offset diff only for files this small
MAX_RANGES = 24       # cap reported differing ranges; a shifted ELF has thousands
PAD = 48              # bytes of context either side of a differing range

# Priority order for the verdict. Earlier causes name a more specific mechanism,
# so they win when several diagnoses fire on the same file; the rest are kept as
# "also", because a file can genuinely have two bugs.
# "reorder" outranks "timestamp" deliberately: if the two outputs have an
# identical byte multiset then nothing was rewritten, only moved, so a date
# cannot be the cause however much the bytes look like one.
PRIORITY = ["gzip-mtime", "ar-order", "ar-metadata", "build-path", "build-host",
            "reorder", "timestamp", "elf-sections", "size", "unknown"]

FIXES = {
    "gzip-mtime": "gzip -n (writes MTIME=0 into the header)",
    "ar-order": "pass the member list in a fixed order, not from ls/wildcard",
    "ar-metadata": "ar D (deterministic: mtime/uid/gid zeroed, mode normalised)",
    "build-path": "compile with a relative source path, or -ffile-prefix-map=",
    "build-host": "do not embed hostname/user; they are not part of the source",
    "timestamp": "honour SOURCE_DATE_EPOCH, or drop the timestamp entirely",
    "reorder": "sort with LC_ALL=C, or sort explicitly instead of trusting ls",
    "elf-sections": "see the per-section sizes below for which section moved",
}


# --------------------------------------------------------------- the variations

# Each axis is a named group of knobs. --no-vary NAME copies A's value into B,
# which is how you bisect "which variation triggers this".
AXES = {
    "path": ["dirname"],
    "time": ["tz", "epoch"],
    "locale": ["locale"],
    "umask": ["umask"],
    "host": ["host", "user"],
    "jobs": ["jobs"],
}


def best_locale():
    """A non-C locale whose collation differs from C, if the system has one.

    This is the one variation that silently becomes a no-op on a minimal system:
    with only C and POSIX installed there is nothing to vary, and the
    locale-dependent-sort class of bug cannot be detected at all. Say so rather
    than pretend.
    """
    try:
        have = subprocess.run(["locale", "-a"], capture_output=True, text=True,
                              timeout=20).stdout.split()
    except (OSError, subprocess.SubprocessError):
        have = []
    low = {h.lower().replace("-", ""): h for h in have}
    for want in ("en_us.utf8", "en_gb.utf8", "de_de.utf8", "c.utf8"):
        if want in low:
            return low[want]
    return "C"


def variants(disabled=()):
    """The two sets of knobs, one axis per line, build A beside build B.

    Written this way so that each axis is one readable line and adding or
    removing one is a one-line change. Anything absent from these dicts is
    simply not varied, and the rest of the file asks `"k" in v` rather than
    assuming every knob exists.
    """
    a, b = dict(name="A"), dict(name="B")
    # B's directory name is deliberately a different LENGTH as well as different
    # content: a leaked path shifts every later offset in the file, which is a
    # much louder signal than a same-length substitution, and it also catches
    # builds that pad or truncate the path.
    a["dirname"], b["dirname"] = "build-a", "b-second-build-dir-with-a-much-longer-name"
    a["tz"], b["tz"] = "UTC", "Asia/Tokyo"
    a["epoch"], b["epoch"] = None, EPOCH_B
    a["locale"], b["locale"] = "C", best_locale()
    a["umask"], b["umask"] = 0o022, 0o077
    a["host"], b["host"] = "builder-one", "buildd-42.example.org"
    a["user"], b["user"] = "alice", "bob"
    a["jobs"], b["jobs"] = 1, 4
    for axis in disabled:
        if axis not in AXES:
            raise SystemExit("unrepro: unknown variation axis %r (have: %s)"
                             % (axis, ", ".join(sorted(AXES))))
        for k in AXES[axis]:
            b[k] = a[k]
    if "time" in disabled:
        # Holding the clock constant has to mean PINNING it, not letting both
        # builds read a free-running wall clock. Two gcc runs 100ms apart
        # disagree about __TIME__ roughly one time in ten, so "--no-vary time"
        # implemented as "neither build gets SOURCE_DATE_EPOCH" produces a
        # bisection that is clean most of the time -- and a flaky bisection is
        # worse than none, because it teaches you to rerun until you like the
        # answer. SOURCE_DATE_EPOCH is the only clock we can pin without
        # libfaketime, which is not in the standard library.
        a["epoch"] = b["epoch"] = EPOCH_B
        a["tz"] = b["tz"] = "UTC"
    return a, b


def build_env(v):
    """A scrubbed environment, so the only differences are the ones we chose."""
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/tmp"),
        "SHELL": "/bin/sh",
        "TERM": "dumb",
    }
    if "tz" in v:
        env["TZ"] = v["tz"]
    if "locale" in v:
        env["LC_ALL"] = env["LANG"] = v["locale"]
    if "host" in v:
        env["HOSTNAME"] = v["host"]
    if "user" in v:
        env["USER"] = env["LOGNAME"] = v["user"]
    if v.get("epoch") is not None:
        env["SOURCE_DATE_EPOCH"] = str(v["epoch"])
    return env


DESCRIBE = (("tz", "TZ=%s"), ("locale", "LC_ALL=%s"), ("umask", "umask=%03o"),
            ("jobs", "-j%d"), ("host", "HOSTNAME=%s"), ("user", "USER=%s"))


def describe(v, root):
    """The report header. Only mentions axes that are actually being varied, so
    it never claims to have varied something that is not in the dict."""
    bits = [root]
    for k, fmt in DESCRIBE:
        if k in v:
            bits.append(fmt % v[k])
    if "epoch" in v:
        bits.append("SOURCE_DATE_EPOCH=%s"
                    % (v["epoch"] if v["epoch"] is not None else "unset"))
    return "  ".join(bits)


# ------------------------------------------------------------------- the builds

def snapshot(root):
    """Content hash, mtime and ctime for every file under root.

    The timestamps are the half that matters for deciding what an output is. A
    perfectly reproducible build rewrites its products to the same bytes they
    already had, so comparing hashes alone cannot tell that apart from a build
    that did nothing -- and the tool then refuses to compare the very outputs
    it was asked about. Rewriting a file moves its mtime whether or not the
    bytes changed, which is the distinction needed.

    ctime is here because mtime can be put back: a tool that rewrites a file
    and then calls utimensat to restore the timestamp is invisible to mtime
    and not to ctime, which no unprivileged caller can set at all. It also
    moves on a chmod, so a build that only changes a mode still gets compared.
    """
    out = {}
    for d, dirs, files in os.walk(root):
        dirs[:] = [x for x in dirs if x != ".git"]
        for f in files:
            p = os.path.join(d, f)
            rel = os.path.relpath(p, root)
            try:
                with open(p, "rb") as fh:
                    h = hashlib.sha256(fh.read()).hexdigest()
                st = os.stat(p)
                out[rel] = (h, st.st_mtime_ns, st.st_ctime_ns)
            except OSError:
                out[rel] = ("unreadable", 0, 0)
    return out


def run_build(src, dest, v, cmd, timeout):
    # .git is not an input to the build and can be enormous; copying it would
    # dominate the runtime and add nothing.
    shutil.copytree(src, dest, ignore=shutil.ignore_patterns(".git"))
    before = snapshot(dest)
    argv = list(cmd) + (["-j%d" % v["jobs"]] if "jobs" in v else [])
    # umask has to be set in the child, after fork and before exec. preexec_fn
    # is the only stdlib hook for that; this process is single-threaded so the
    # usual warning about it does not apply.
    p = subprocess.run(argv, cwd=dest, env=build_env(v), timeout=timeout,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                       preexec_fn=((lambda: os.umask(v["umask"]))
                                   if "umask" in v else None))
    after = snapshot(dest)
    # shortcut: an output is "a file inside the tree the build rewrote",
    # judged by content hash, mtime or ctime. That misses a build writing
    # outside its own tree, and counts intermediates like .o as outputs --
    # which turns out to help (in t/cases/ar-stamp the clean .o files are what
    # prove the archive headers are the only problem) but was an accident.
    # It also assumes the filesystem has timestamps finer than the build is
    # fast: on one with whole-second mtime and ctime, a rewrite inside the same
    # second is invisible. Upgrade: honour an explicit output list, or diff the
    # install tree from a DESTDIR staged install.
    outputs = sorted(r for r, s in after.items() if before.get(r) != s)
    return p.returncode, p.stdout.decode("utf-8", "replace"), outputs


# --------------------------------------------------------------- byte machinery

def dranges(a, b, merge=4):
    """Differing byte ranges, with near-adjacent ones merged."""
    n = min(len(a), len(b))
    out, i = [], 0
    while i < n:
        if a[i] != b[i]:
            j = i
            while j < n and a[j] != b[j]:
                j += 1
            out.append([i, j])
            i = j
        else:
            i += 1
    if len(a) != len(b):
        out.append([n, max(len(a), len(b))])
    merged = []
    for r in out:
        if merged and r[0] - merged[-1][1] <= merge:
            merged[-1][1] = r[1]
        else:
            merged.append(r)
    return [(lo, hi) for lo, hi in merged]


def window(buf, lo, hi, pad=PAD):
    return buf[max(0, lo - pad):min(len(buf), hi + pad)]


def utc(e):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(e))


def hexline(buf, off, n=16):
    chunk = buf[off:off + n]
    h = " ".join("%02x" % c for c in chunk)
    t = "".join(chr(c) if 32 <= c < 127 else "." for c in chunk)
    return "%08x  %-47s  %s" % (off, h, t)


def hexdiff(a, b, ranges):
    """Byte-offset-level diff. Small files get this in full."""
    out = []
    for lo, hi in ranges:
        start = max(0, (lo - 8) & ~0xF)
        end = min(max(len(a), len(b)), hi + 8)
        out.append("  @ 0x%x..0x%x (%d byte%s)"
                   % (lo, hi, hi - lo, "" if hi - lo == 1 else "s"))
        for off in range(start, end, 16):
            out.append("    A " + hexline(a, off))
            out.append("    B " + hexline(b, off))
    return out


# ------------------------------------------------------------------------- ELF

def elf_sections(buf):
    """Parse the ELF64-LE section table directly.

    Thirty lines of struct beats shelling out to readelf and parsing its column
    layout, which overflows and loses alignment on long section names.

    # shortcut: ELF64 little-endian only -- that is every machine this runs on
    # today. 32-bit and big-endian return [] and fall back to plain byte
    # diffing, which still produces a correct (just less located) verdict.
    """
    if len(buf) < 0x40 or buf[:4] != b"\x7fELF" or buf[4] != 2 or buf[5] != 1:
        return []
    try:
        shoff, = struct.unpack_from("<Q", buf, 0x28)
        shentsize, shnum, shstrndx = struct.unpack_from("<HHH", buf, 0x3a)
        if not shoff or shnum == 0 or shoff + shnum * shentsize > len(buf):
            return []
        raw = []
        for i in range(shnum):
            o = shoff + i * shentsize
            nameoff, typ = struct.unpack_from("<II", buf, o)
            off, size = struct.unpack_from("<QQ", buf, o + 0x18)
            raw.append((nameoff, typ, off, size))
        if shstrndx >= shnum:
            return []
        stroff, strsize = raw[shstrndx][2], raw[shstrndx][3]
        strtab = buf[stroff:stroff + strsize]
    except (struct.error, IndexError):
        return []
    secs = []
    for nameoff, typ, off, size in raw:
        end = strtab.find(b"\0", nameoff)
        name = strtab[nameoff:end].decode("ascii", "replace") if 0 <= nameoff < len(strtab) else "?"
        secs.append((name, typ, off, size))
    return secs


def section_at(secs, off):
    for name, typ, o, size in secs:
        if typ != 8 and o <= off < o + size:   # 8 == SHT_NOBITS: no file bytes
            return name
    return None


# -------------------------------------------------------------------------- ar

# shortcut: ar only. tar and cpio have the same shape of bug (member order,
# mtime, uid/gid/uname/gname, mode, pax headers) and no diagnoser here, so a
# .tar.gz gets its gzip wrapper explained and nothing about its contents.
# Ceiling: the format most source releases actually ship in. Upgrade: a
# tar_members() parsing the 512-byte ustar header, same shape as ar_members.

AR_MAGIC = b"!<arch>\n"


def ar_members(buf):
    if buf[:8] != AR_MAGIC:
        return None
    out, i = [], 8
    while i + 60 <= len(buf):
        h = buf[i:i + 60]
        if h[58:60] != b"`\n":
            break
        try:
            size = int(h[48:58].decode("ascii").strip())
        except ValueError:
            break
        # GNU ar terminates member names with "/"; strip it for readability but
        # leave the special "/" and "//" members alone.
        nm = h[0:16].decode("latin-1").rstrip()
        if len(nm) > 1 and nm.endswith("/") and nm != "//":
            nm = nm[:-1]
        out.append(dict(name=nm,
                        mtime=h[16:28].decode("latin-1").strip(),
                        uid=h[28:34].decode("latin-1").strip(),
                        gid=h[34:40].decode("latin-1").strip(),
                        mode=h[40:48].decode("latin-1").strip(),
                        size=size))
        i += 60 + size + (size & 1)
    return out


# ------------------------------------------------------------------------ gzip

def gz_header(buf):
    if len(buf) < 10 or buf[:2] != b"\x1f\x8b":
        return None
    flg = buf[3]
    mtime, = struct.unpack_from("<I", buf, 4)
    i = 10
    try:
        if flg & 4:
            xlen, = struct.unpack_from("<H", buf, i)
            i += 2 + xlen
        name = None
        if flg & 8:
            e = buf.find(b"\0", i)
            name, i = buf[i:e].decode("latin-1"), e + 1
        if flg & 16:
            i = buf.find(b"\0", i) + 1
        if flg & 2:
            i += 2
    except struct.error:
        return None
    return dict(flg=flg, mtime=mtime, name=name, payload=i)


# --------------------------------------------------------------- date sniffing

DATE_RES = [
    (re.compile(rb"[A-Z][a-z]{2} [ \d]\d \d{4}"), "C __DATE__ form 'Mon DD YYYY'"),
    (re.compile(rb"\d{4}-[01]\d-[0-3]\d"), "ISO date"),
    (re.compile(rb"[A-Z][a-z]{2} [A-Z][a-z]{2} +\d{1,2} "), "date(1) output 'Day Mon DD'"),
    (re.compile(rb"\b[0-2]\d:[0-5]\d:[0-5]\d\b"), "clock time 'HH:MM:SS'"),
]


def text_dates(buf):
    found = []
    for rx, label in DATE_RES:
        for m in rx.finditer(buf):
            found.append((m.group(0).decode("latin-1"), label))
    return found


def epoch_nums(buf, lo, hi):
    """Little-endian integers near a differing range that look like a date.

    The TEXTISH guard is the whole difference between a diagnosis and a
    coincidence: without it every text file on earth is full of "timestamps".
    Found by running this tool against its own ls-order case, which it
    confidently mis-diagnosed as a clock bug.
    """
    seen, out = set(), []
    for p in range(max(0, lo - 7), min(len(buf), hi + 1)):
        for fmt, n in (("<I", 4), ("<Q", 8)):
            if p + n > len(buf):
                continue
            if all(c in TEXTISH for c in buf[p:p + n]):
                continue
            # A value with two or more zero bytes in its low word is a round
            # number -- 0x34000000, 0x40000000 -- which is what shifted machine
            # code and padding look like, not what a clock looks like. Real
            # epochs in this era have no zero byte at all.
            if buf[p:p + 4].count(0) > 1:
                continue
            v, = struct.unpack_from(fmt, buf, p)
            if PLAUSIBLE[0] <= v <= PLAUSIBLE[1] and v not in seen:
                seen.add(v)
                out.append((p, n, v))
    return out


def pick_dates(da, db):
    """Prefer matches that actually DIFFER between the two sides.

    Both builds ran on the same day, so "Tue Oct  6" matches in both and is not
    the finding; the clock time beside it is. Reporting the shared prefix makes
    the tool look like it is guessing.
    """
    sa, sb = {x[0] for x in da}, {x[0] for x in db}
    pa = [x for x in da if x[0] not in sb] or da
    pb = [x for x in db if x[0] not in sa] or db
    return pa[0], pb[0]


def epoch_forms(e):
    """Every spelling of one epoch that we have seen leak into a build output.

    # shortcut: rendered in UTC only, which is what gcc does for
    # SOURCE_DATE_EPOCH. A build that formats it in the local timezone instead
    # would not match any of these and would fall back to the weaker "both sides
    # parse as a date" wording. Upgrade: render under both varied TZ values,
    # which needs the TZ pair threaded into ctx.
    """
    t = time.gmtime(e)
    return [(time.strftime("%b %e %Y", t).encode(), "__DATE__ form"),
            (time.strftime("%H:%M:%S", t).encode(), "__TIME__ form"),
            (time.strftime("%Y-%m-%d", t).encode(), "ISO form"),
            (str(e).encode(), "decimal seconds"),
            (struct.pack("<I", e), "4-byte LE binary")]


# ------------------------------------------------------------------- diagnosis

def diagnose(rel, a, b, ctx):
    """Classify one differing file. Returns a finding dict."""
    f = {"path": rel, "status": "differs", "size": [len(a), len(b)],
         "cause": None, "detail": [], "also": [], "ranges": [], "hexdump": []}
    if a == b:
        # Defensive. compare() never routes identical files here, but diagnose()
        # is called directly too, and every diagnoser below assumes there is at
        # least one differing range to talk about.
        f["status"], f["n_ranges"] = "identical", 0
        return f
    rs = dranges(a, b)
    f["ranges"] = [[lo, hi] for lo, hi in rs[:MAX_RANGES]]
    f["n_ranges"] = len(rs)
    cands = {}

    def add(cause, lines):
        cands.setdefault(cause, lines)

    secs_a, secs_b = elf_sections(a), elf_sections(b)
    is_elf = bool(secs_a) and bool(secs_b)
    sa = {n: sz for n, _, _, sz in secs_a}
    sb = {n: sz for n, _, _, sz in secs_b}
    sec_lines = ["  section %-20s size A=%s B=%s"
                 % (n, sa.get(n, "absent"), sb.get(n, "absent"))
                 for n in sorted(set(sa) | set(sb)) if sa.get(n) != sb.get(n)]
    # Once anything has changed LENGTH, every byte after it sits at a different
    # offset in the two files, so comparing "the integer at offset X" compares
    # two unrelated things. Sniffing for binary timestamps under those
    # conditions invents dates out of shifted machine code -- it did exactly
    # that on four real repos before this flag existed. Text matches survive,
    # because a string is still a string wherever it landed.
    shifted = len(a) != len(b) or bool(sec_lines)

    # --- gzip header, before anything else: a .gz whose only difference is the
    # 4-byte MTIME field is the single most common packaging bug there is.
    ha, hb = gz_header(a), gz_header(b)
    if ha and hb and ha["mtime"] != hb["mtime"]:
        same = a[ha["payload"]:] == b[hb["payload"]:]
        add("gzip-mtime", [
            "gzip header MTIME (offset 0x04..0x07) differs:",
            "  A %d  (%s)" % (ha["mtime"], utc(ha["mtime"]) if ha["mtime"] else "zero / not set"),
            "  B %d  (%s)" % (hb["mtime"], utc(hb["mtime"]) if hb["mtime"] else "zero / not set"),
            "deflate payload after the header is %s"
            % ("byte-identical, so the header is the ONLY difference" if same
               else "also different, so there is a second cause below"),
        ])

    # --- ar archive member table
    ma, mb = ar_members(a), ar_members(b)
    if ma is not None and mb is not None:
        na, nb = [m["name"] for m in ma], [m["name"] for m in mb]
        if na != nb:
            kind = ("same members, different order" if sorted(na) == sorted(nb)
                    else "different member sets")
            add("ar-order", ["ar member order differs (%s):" % kind,
                             "  A %s" % " ".join(na), "  B %s" % " ".join(nb)])
        if sorted(na) == sorted(nb):
            by_b = {m["name"]: m for m in mb}
            lines, fields = [], set()
            for m in ma:
                o = by_b.get(m["name"])
                if not o:
                    continue
                for k in ("mtime", "uid", "gid", "mode"):
                    if m[k] != o[k]:
                        fields.add(k)
                        extra = ""
                        if k == "mtime":
                            try:
                                extra = ("   (%s vs %s)"
                                         % (utc(int(m[k] or 0)), utc(int(o[k] or 0))))
                            except ValueError:
                                pass
                        lines.append("  member %-16s %-5s  A=%-12s B=%-12s%s"
                                     % (m["name"], k, m[k] or '""', o[k] or '""', extra))
            # shortcut: member payloads are compared as whole blobs, so a
            # verdict is "libfoo.a" and never "libfoo.a(bar.o) .comment".
            # Upgrade: recurse -- hand each member pair back to diagnose().
            if fields:
                add("ar-metadata",
                    ["ar member header field(s) differ: %s"
                     % ", ".join(sorted(fields))] + lines[:12] +
                    ["member payloads are identical"
                     if all(x["size"] == by_b[x["name"]]["size"] for x in ma)
                     else "member sizes differ too"])

    # --- a build path baked into the artifact
    for pa, pb in ctx.get("paths", []):
        ea, eb = pa.encode(), pb.encode()
        hit_a, hit_b = a.find(ea), b.find(eb)
        if hit_a >= 0 or hit_b >= 0:
            lines = ["the build directory path is embedded in the output:"]
            if hit_a >= 0:
                lines.append("  A contains %r at offset 0x%x%s"
                             % (pa, hit_a, sec_note(secs_a, hit_a)))
            if hit_b >= 0:
                lines.append("  B contains %r at offset 0x%x%s"
                             % (pb, hit_b, sec_note(secs_b, hit_b)))
            lines.append("the two builds differ only in where they ran, so this "
                         "string is machine identity, not source")
            add("build-path", lines)
            break

    # --- build host / user identity
    for key, label in (("hosts", "hostname"), ("users", "user name")):
        for va, vb in ctx.get(key, []):
            if va == vb:
                continue
            ha_, hb_ = a.find(va.encode()), b.find(vb.encode())
            if ha_ >= 0 or hb_ >= 0:
                add("build-host",
                    ["the build %s is embedded in the output:" % label] +
                    (["  A contains %r at offset 0x%x%s" % (va, ha_, sec_note(secs_a, ha_))]
                     if ha_ >= 0 else []) +
                    (["  B contains %r at offset 0x%x%s" % (vb, hb_, sec_note(secs_b, hb_))]
                     if hb_ >= 0 else []))

    # --- a timestamp
    tlines = []
    for lo, hi in rs[:8]:
        wa, wb = window(a, lo, hi), window(b, lo, hi)
        da, db = text_dates(wa), text_dates(wb)
        if da and db:
            ma_, mb_ = pick_dates(da, db)
            tlines.append("  at 0x%x%s: A text %r (%s) / B text %r"
                          % (lo, sec_note(secs_a, lo), ma_[0], ma_[1], mb_[0]))
        if shifted:
            continue
        ea, eb = epoch_nums(a, lo, hi), epoch_nums(b, lo, hi)
        if ea and eb and ea[0][2] != eb[0][2]:
            tlines.append("  at 0x%x%s: A %d-byte LE %d (%s) / B %d (%s)"
                          % (ea[0][0], sec_note(secs_a, ea[0][0]), ea[0][1],
                             ea[0][2], utc(ea[0][2]), eb[0][2], utc(eb[0][2])))
    proof = []
    eb_epoch = ctx.get("epoch_b")
    if eb_epoch is not None:
        for form, label in epoch_forms(eb_epoch):
            if form in b and form not in a:
                proof.append("  B contains the %s of SOURCE_DATE_EPOCH=%d: %r"
                             % (label, eb_epoch, form))
    if tlines or proof:
        head = ["the differing bytes decode as a date:"] if tlines else []
        tail = (["this is conclusive: that value came from the clock we handed "
                 "build B, so the build embeds its own build time"] if proof else
                ["both sides parse as plausible dates, so this is the clock, "
                 "not the source"])
        add("timestamp", head + tlines + proof + tail)

    # --- reordering: same content, different sequence
    if sorted(a) == sorted(b):
        la, lb = a.splitlines(), b.splitlines()
        if sorted(la) == sorted(lb):
            add("reorder", [
                "the two outputs are a permutation of each other: identical "
                "byte multiset, identical set of %d line(s), different order." % len(la),
                "  A order: %s" % " | ".join(x.decode("latin-1", "replace") for x in la[:6]),
                "  B order: %s" % " | ".join(x.decode("latin-1", "replace") for x in lb[:6]),
                "we varied LC_ALL from %r to %r; glibc collation ignores case "
                "and punctuation outside C, so ls/sort/* return a different "
                "sequence" % (ctx.get("locale_a"), ctx.get("locale_b")),
            ])
        else:
            add("reorder", ["identical byte multiset, different byte order"])

    # --- ELF section table, as a locator and as a cause of last resort
    if is_elf:
        if sec_lines:
            add("elf-sections",
                ["ELF section sizes differ, so the difference is a layout shift, "
                 "not an in-place byte swap:"] + sec_lines[:12])
        else:
            hit = [section_at(secs_a, lo) for lo, _ in rs[:MAX_RANGES]]
            seen = sorted({h for h in hit if h})
            if seen:
                f["detail"].append("ELF section table is identical; differing "
                                   "bytes fall in: %s" % ", ".join(seen))

    if len(a) != len(b) and not cands:
        add("size", ["file lengths differ: A %d bytes, B %d bytes, and no known "
                     "mechanism matched" % (len(a), len(b))])

    if not cands:
        lo, hi = rs[0]
        add("unknown", [
            "no known mechanism matched. %d differing range(s); first at "
            "0x%x..0x%x%s:" % (len(rs), lo, hi, sec_note(secs_a, lo)),
            "    A " + hexline(a, max(0, lo - 8) & ~0xF),
            "    B " + hexline(b, max(0, lo - 8) & ~0xF),
            "report this offset; it is a real reproducibility bug we cannot "
            "yet classify, which is the honest answer",
        ])

    order = [c for c in PRIORITY if c in cands]
    f["cause"] = order[0]
    f["detail"] = f["detail"] + cands[order[0]]
    if FIXES.get(order[0]):
        f["detail"].append("fix: " + FIXES[order[0]])
    for c in order[1:]:
        f["also"].append({"cause": c, "detail": cands[c]})
    if max(len(a), len(b)) <= HEX_LIMIT:
        f["hexdump"] = hexdiff(a, b, rs[:MAX_RANGES])
    return f


def sec_note(secs, off):
    n = section_at(secs, off) if secs else None
    return " (section %s)" % n if n else ""


# -------------------------------------------------------------------- comparing

def read(p):
    with open(p, "rb") as fh:
        return fh.read()


def compare(root_a, root_b, rels, ctx):
    findings = []
    for rel in sorted(rels):
        pa, pb = os.path.join(root_a, rel), os.path.join(root_b, rel)
        ea, eb = os.path.exists(pa), os.path.exists(pb)
        if not ea or not eb:
            findings.append({"path": rel, "status": "only-in-%s" % ("A" if ea else "B"),
                             "cause": "missing", "detail": [
                                 "produced by only one of the two builds, which is "
                                 "itself a reproducibility failure"],
                             "also": [], "ranges": [], "hexdump": []})
            continue
        a, b = read(pa), read(pb)
        if a == b:
            findings.append({"path": rel, "status": "identical", "cause": None,
                             "detail": [], "also": [], "ranges": [], "hexdump": []})
        else:
            findings.append(diagnose(rel, a, b, ctx))
    return findings


def tree_files(root):
    out = set()
    for d, dirs, files in os.walk(root):
        dirs[:] = [x for x in dirs if x != ".git"]
        for f in files:
            out.add(os.path.relpath(os.path.join(d, f), root))
    return out


# ----------------------------------------------------------------------- report

def render(res):
    out = []
    w = out.append
    if res.get("source"):
        w("unrepro: %s" % res["source"])
    for v in res.get("builds", []):
        w("  build %s  %s" % (v["name"], v["desc"]))
    if res.get("notes"):
        for n in res["notes"]:
            w("  note: %s" % n)
    w("")
    diff = [f for f in res["files"] if f["status"] != "identical"]
    w("%d output file(s) compared, %d differ" % (len(res["files"]), len(diff)))
    w("")
    for f in res["files"]:
        if f["status"] == "identical":
            w("  %-28s ok" % f["path"])
            continue
        w("  %-28s %-9s %s" % (f["path"], f["status"].upper(), f["cause"]))
        for line in f["detail"]:
            w("      " + line)
        for alt in f["also"]:
            w("      also: %s" % alt["cause"])
            for line in alt["detail"]:
                w("        " + line)
        if f["hexdump"]:
            w("      byte-offset diff:")
            for line in f["hexdump"]:
                w("    " + line)
        w("")
    if res["verdict"] == "reproducible":
        w("VERDICT: REPRODUCIBLE across all varied conditions (%d output(s))"
          % len(res["files"]))
    else:
        w("VERDICT: NOT REPRODUCIBLE -- %d of %d output(s) differ: %s"
          % (len(diff), len(res["files"]),
             ", ".join(sorted({f["cause"] for f in diff}))))
    return "\n".join(out)


# ------------------------------------------------------------------------- CLI

def cmd_check(args):
    src = os.path.abspath(args.dir)
    if not os.path.isdir(src):
        print("unrepro: no such directory: %s" % src, file=sys.stderr)
        return 2
    disabled = [x for x in (args.no_vary or "").split(",") if x]
    a, b = variants(disabled)
    notes = []
    if b["locale"] == a["locale"]:
        notes.append("locale variation is INERT: no non-C locale is installed, "
                     "so locale-dependent sort bugs cannot be detected here")
    cmd = args.cmd.split() if args.cmd else ["make"]
    tmp = tempfile.mkdtemp(prefix="unrepro.")
    try:
        roots, logs = {}, {}
        outs = {}
        # --no-vary path means literally the same directory, which is only
        # possible because the builds are sequential: build A, move its tree
        # aside, then build B at the identical path. Faking it with two
        # same-length names would leave a one-character difference that still
        # leaks, so the bisection would not come back clean and the flag would
        # be useless for the one job it has.
        same_dir = a["dirname"] == b["dirname"]
        for v in (a, b):
            root = os.path.join(tmp, v["dirname"])
            rc, log, o = run_build(src, root, v, cmd, args.timeout)
            roots[v["name"]], logs[v["name"]], outs[v["name"]] = root, log, o
            if rc != 0:
                print("unrepro: build %s failed (exit %d) in %s\n%s"
                      % (v["name"], rc, root, log), file=sys.stderr)
                return 2
            if v is a and same_dir:
                roots["A"] = os.path.join(tmp, "a-saved")
                shutil.move(root, roots["A"])
        rels = set(outs["A"]) | set(outs["B"])
        # With the path held constant there is no pair of path strings to look
        # for, and pretending otherwise would make every output "contain the
        # build path" trivially.
        ctx = {}
        ctx["paths"] = [] if same_dir else [(roots["A"], roots["B"])]
        ctx["hosts"] = [(a["host"], b["host"])] if "host" in a else []
        ctx["users"] = [(a["user"], b["user"])] if "user" in a else []
        ctx["epoch_b"] = b.get("epoch")
        ctx["locale_a"], ctx["locale_b"] = a.get("locale"), b.get("locale")
        # HARD INVARIANT: never report clean on a build we did not actually
        # observe. A source tree with its products already committed (or just
        # left lying around) makes `make` a no-op, and the honest verdict for
        # zero compared outputs is "inconclusive", not "reproducible". This is
        # the exact false negative the tool exists to avoid, and the first real
        # repo it was pointed at hit it.
        if not rels:
            msg = ("unrepro: the build rewrote no files, so there is nothing "
                   "to compare.\n"
                   "  An output is any file the build rewrote, including one it "
                   "rewrote to the bytes\n"
                   "  already there, so this is not the "
                   "products-already-in-the-tree case: the\n"
                   "  build genuinely had nothing to do.\n"
                   "  Retry with --cmd 'make -B' to force it, or point it at a "
                   "clean checkout.\n"
                   "  Build A said:\n%s"
                   % "".join("    " + l + "\n" for l in
                             logs["A"].splitlines()[:10] or ["(no output)"]))
            if args.json:
                print(json.dumps({"source": src, "verdict": "inconclusive",
                                  "reason": "no outputs observed",
                                  "files": [], "notes": notes}, indent=2))
            else:
                print(msg, file=sys.stderr)
            return 2
        files = compare(roots["A"], roots["B"], rels, ctx)
        diff = [f for f in files if f["status"] != "identical"]
        res = {"source": src,
               "builds": [{"name": v["name"],
                           "desc": describe(v, os.path.join(tmp, v["dirname"]))}
                          for v in (a, b)],
               "notes": notes,
               "varied": sorted(set(AXES) - set(disabled)),
               "files": files,
               "verdict": "not-reproducible" if diff else "reproducible"}
        if args.json:
            print(json.dumps(res, indent=2))
        else:
            print(render(res))
        return 1 if diff else 0
    finally:
        if args.keep:
            print("kept build trees under %s" % tmp, file=sys.stderr)
        else:
            shutil.rmtree(tmp, ignore_errors=True)


def cmd_diff(args):
    a, b = os.path.abspath(args.a), os.path.abspath(args.b)
    # The two roots are the only path hints available here, which is enough for
    # build-path detection if a and b really are two build trees. Without a
    # known SOURCE_DATE_EPOCH the timestamp diagnosis falls back to "both sides
    # parse as dates", which is weaker but still a named cause.
    ctx = {"paths": [(a, b)], "hosts": [], "users": [], "epoch_b": None,
           "locale_a": None, "locale_b": None}
    if os.path.isdir(a) and os.path.isdir(b):
        rels = tree_files(a) | tree_files(b)
        files = compare(a, b, rels, ctx)
    elif os.path.isfile(a) and os.path.isfile(b):
        ba, bb = read(a), read(b)
        rel = os.path.basename(a)
        files = [diagnose(rel, ba, bb, ctx) if ba != bb else
                 {"path": rel, "status": "identical", "cause": None, "detail": [],
                  "also": [], "ranges": [], "hexdump": []}]
    else:
        print("unrepro: %s and %s must both be files or both be directories"
              % (a, b), file=sys.stderr)
        return 2
    diff = [f for f in files if f["status"] != "identical"]
    res = {"source": "%s  vs  %s" % (a, b), "builds": [],
           "notes": ["comparing two existing trees: no variation was applied by "
                     "unrepro, so a clean result here proves only that these two "
                     "particular outputs match"],
           "files": files,
           "verdict": "not-reproducible" if diff else "reproducible"}
    print(json.dumps(res, indent=2) if args.json else render(res))
    return 1 if diff else 0


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="unrepro",
        description="build twice under varied conditions and diagnose why the "
                    "outputs differ")
    sub = p.add_subparsers(dest="cmd_name", required=True)

    c = sub.add_parser("check", help="build DIR twice with varied conditions")
    c.add_argument("dir")
    c.add_argument("--cmd", help="build command (default: make)")
    c.add_argument("--no-vary", help="comma-separated axes to hold constant: %s"
                                     % ",".join(sorted(AXES)))
    c.add_argument("--timeout", type=int, default=600)
    c.add_argument("--keep", action="store_true", help="keep the build trees")
    c.add_argument("--json", action="store_true")
    c.set_defaults(fn=cmd_check)

    d = sub.add_parser("diff", help="compare and diagnose two files or trees")
    d.add_argument("a")
    d.add_argument("b")
    d.add_argument("--json", action="store_true")
    d.set_defaults(fn=cmd_diff)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
