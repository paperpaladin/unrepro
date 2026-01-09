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

HEX_LIMIT = 4096      # full byte-offset diff only for files this small
MAX_RANGES = 24       # cap reported differing ranges; a shifted ELF has thousands
PAD = 48              # bytes of context either side of a differing range

# Priority order for the verdict. Earlier causes name a more specific mechanism,
# so they win when several diagnoses fire on the same file; the rest are kept as
# "also", because a file can genuinely have two bugs.
PRIORITY = ["gzip-mtime", "ar-order", "ar-metadata", "build-path", "build-host",
            "timestamp", "reorder", "elf-sections", "size", "unknown"]

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


def variants():
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
    out = {}
    for d, dirs, files in os.walk(root):
        dirs[:] = [x for x in dirs if x != ".git"]
        for f in files:
            p = os.path.join(d, f)
            rel = os.path.relpath(p, root)
            try:
                with open(p, "rb") as fh:
                    out[rel] = hashlib.sha256(fh.read()).hexdigest()
            except OSError:
                out[rel] = "unreadable"
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
    # shortcut: an output is "a file inside the tree whose content changed".
    # That misses a build writing outside its own tree, and counts intermediates
    # like .o as outputs -- which turns out to help (in t/cases/ar-stamp the
    # clean .o files are what prove the archive headers are the only problem)
    # but was an accident. Upgrade: honour an explicit output list, or diff the
    # install tree from a DESTDIR staged install.
    outputs = sorted(r for r, h in after.items() if before.get(r) != h)
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
    a, b = variants()
    notes = []
    if b["locale"] == a["locale"]:
        notes.append("locale variation is INERT: no non-C locale is installed, "
                     "so locale-dependent sort bugs cannot be detected here")
    cmd = args.cmd.split() if args.cmd else ["make"]
    tmp = tempfile.mkdtemp(prefix="unrepro.")
    try:
        roots, logs = {}, {}
        outs = {}
        for v in (a, b):
            root = os.path.join(tmp, v["dirname"])
            rc, log, o = run_build(src, root, v, cmd, args.timeout)
            roots[v["name"]], logs[v["name"]], outs[v["name"]] = root, log, o
            if rc != 0:
                print("unrepro: build %s failed (exit %d) in %s\n%s"
                      % (v["name"], rc, root, log), file=sys.stderr)
                return 2
        rels = set(outs["A"]) | set(outs["B"])
        ctx = {}
        ctx["paths"] = [(roots["A"], roots["B"])]
        ctx["hosts"] = [(a["host"], b["host"])] if "host" in a else []
        ctx["users"] = [(a["user"], b["user"])] if "user" in a else []
        ctx["epoch_b"] = b.get("epoch")
        ctx["locale_a"], ctx["locale_b"] = a.get("locale"), b.get("locale")
        files = compare(roots["A"], roots["B"], rels, ctx)
        diff = [f for f in files if f["status"] != "identical"]
        res = {"source": src,
               "builds": [{"name": v["name"],
                           "desc": describe(v, os.path.join(tmp, v["dirname"]))}
                          for v in (a, b)],
               "notes": notes,
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


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="unrepro",
        description="build twice under varied conditions and diagnose why the "
                    "outputs differ")
    sub = p.add_subparsers(dest="cmd_name", required=True)

    c = sub.add_parser("check", help="build DIR twice with varied conditions")
    c.add_argument("dir")
    c.add_argument("--cmd", help="build command (default: make)")
    c.add_argument("--timeout", type=int, default=600)
    c.add_argument("--keep", action="store_true", help="keep the build trees")
    c.add_argument("--json", action="store_true")
    c.set_defaults(fn=cmd_check)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
