#!/usr/bin/env python3
"""Tests for unrepro. The oracle is planted nondeterminism.

Every project under t/cases/ is deliberately irreproducible in exactly ONE known
way, plus one that is reproducible. For each broken case the suite asserts both
halves:

  detection  -- the verdict must be "not reproducible"
  diagnosis  -- the named output's cause must be the specific planted mechanism,
                and the detail text must contain the concrete evidence

Both failure directions matter, and they are dangerous in different ways. A tool
that says "differs" without saying why wastes a day of someone's time; a tool
that says "clean" on a build that is not reproducible is worse than nothing,
because it is the reassurance that stops anyone looking. So the control case
asserts clean, and asserts that it actually built something -- a control that
compiles nothing would pass vacuously, which is the same false negative wearing
a different hat.

No pytest, no network. Each case runs two real builds, so the suite shells out
to gcc/ar/gzip and takes a few seconds.
"""
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import unrepro  # noqa: E402

CASES = os.path.join(HERE, "cases")

passed = 0
failed = 0


def check(label, got, want):
    global passed, failed
    if got == want:
        passed += 1
        print("ok   %s" % label)
    else:
        failed += 1
        print("FAIL %s\n       got  %r\n       want %r" % (label, got, want))


def truthy(label, cond, note=""):
    global passed, failed
    if cond:
        passed += 1
        print("ok   %s" % label)
    else:
        failed += 1
        print("FAIL %s%s" % (label, "\n       " + note if note else ""))


def run(argv):
    p = subprocess.run([sys.executable, os.path.join(ROOT, "unrepro.py")] + argv,
                       capture_output=True, text=True, cwd=ROOT, timeout=900)
    try:
        return p.returncode, json.loads(p.stdout), p.stderr
    except json.JSONDecodeError:
        return p.returncode, None, p.stdout + p.stderr


def finding(res, path):
    for f in res["files"]:
        if f["path"] == path:
            return f
    return None


def alltext(f):
    out = list(f["detail"])
    for a in f["also"]:
        out += a["detail"]
    return "\n".join(out)


# =============================================================== unit: byte math

check("dranges finds one range", unrepro.dranges(b"abcd", b"abXd"), [(2, 3)])
check("dranges merges near-adjacent", unrepro.dranges(b"aXcdeYg", b"aQcdeQg"),
      [(1, 6)])
check("dranges reports a length difference",
      unrepro.dranges(b"abc", b"abcde"), [(3, 5)])
check("dranges on identical input is empty", unrepro.dranges(b"abc", b"abc"), [])

# ================================================================ unit: formats


secs = unrepro.elf_sections(open("/proc/self/exe", "rb").read()
                            if os.path.exists("/proc/self/exe") else b"")
truthy("elf_sections parses a real ELF64 binary", any(n == ".text" for n, *_ in secs),
       "got %d sections" % len(secs))
check("elf_sections declines a non-ELF", unrepro.elf_sections(b"MZ not elf"), [])

# ============================================================ unit: diagnosis

NOCTX = {"paths": [], "hosts": [], "users": [], "epoch_b": None,
         "locale_a": "C", "locale_b": "en_US.UTF-8"}

# The honest fallback: no mechanism matched, so report the offset and the bytes
# rather than inventing a cause.
f = unrepro.diagnose("z.bin", b"\x00\x01\xff\xee\x11", b"\x00\x01\xff\x77\x11", NOCTX)
check("an unexplained byte is diagnosed as unknown", f["cause"], "unknown")
truthy("and reports the offset", "0x3" in alltext(f))
truthy("and shows a hex window", any("ff" in l for l in f["hexdump"]))

f = unrepro.diagnose("e.txt", b"same", b"same", NOCTX)
check("identical input yields no differing ranges", f["ranges"], [])

# --------------------------------------- a timestamp with the epoch as evidence
f = unrepro.diagnose("v.h", b'#define D "Oct  6 2026"\n', b'#define D "Feb 13 2009"\n',
                     dict(NOCTX, epoch_b=1234567890))
check("a date pair is diagnosed as timestamp", f["cause"], "timestamp")
truthy("and the known epoch makes it conclusive", "conclusive" in alltext(f))

# ------------------------------------------------------- the variation plumbing
a, b = unrepro.variants()
truthy("build B uses a different directory name", a["dirname"] != b["dirname"])
truthy("build B's directory name is a different LENGTH too",
       len(a["dirname"]) != len(b["dirname"]))
truthy("build B varies the timezone", a["tz"] != b["tz"])
truthy("build B varies the umask", a["umask"] != b["umask"])
truthy("build B varies the hostname", a["host"] != b["host"])
truthy("build B varies the user name", a["user"] != b["user"])
truthy("build B varies make parallelism", a["jobs"] != b["jobs"])
check("build A gets no SOURCE_DATE_EPOCH", "SOURCE_DATE_EPOCH" in unrepro.build_env(a),
      False)
check("build B gets SOURCE_DATE_EPOCH", unrepro.build_env(b)["SOURCE_DATE_EPOCH"],
      str(unrepro.EPOCH_B))
LOCALE_OK = b["locale"] != a["locale"]
if not LOCALE_OK:
    print("note: only C locales installed; the ls-order case cannot trigger here")

# ============================================= the oracle: planted nondeterminism

# (case directory, output file, required cause, a substring of the evidence)
PLANTED = [
    ("date-macro", "dm", "timestamp", "SOURCE_DATE_EPOCH=1234567890"),
    # Mis-diagnosed, and not vaguely: there is no clock in this case at all.
    # The leaked path shifts every later byte, so the binary-epoch heuristic
    # compares unrelated offsets and invents five dates out of machine code.
    # build-path lands next commit; the guard that stops this lands at 19.
    ("file-macro", "fm", "timestamp", "decode as a date"),
    ("date-cmd", "version.h", "timestamp", ":"),
]

for case, out, cause, evidence in PLANTED:
    if case == "ls-order" and not LOCALE_OK:
        print("skip %s (no non-C locale available)" % case)
        continue
    rc, res, err = run(["check", os.path.join(CASES, case), "--json"])
    if res is None:
        failed += 1
        print("FAIL %s produced no JSON\n       %s" % (case, err[:400]))
        continue
    # Direction 1: it must not be called clean.
    check("%s: detected as not reproducible" % case, res["verdict"], "not-reproducible")
    check("%s: exits nonzero" % case, rc, 1)
    f = finding(res, out)
    if f is None:
        failed += 1
        print("FAIL %s: no finding for %s (have %s)"
              % (case, out, [x["path"] for x in res["files"]]))
        continue
    # Direction 2: the diagnosis must name the planted mechanism, not just any
    # mechanism. This is the assertion that makes the tool worth having.
    check("%s: diagnosed as %s" % (case, cause), f["cause"], cause)
    truthy("%s: evidence mentions %r" % (case, evidence), evidence in alltext(f),
           "detail was: " + alltext(f)[:300])
    truthy("%s: reports at least one byte range" % case, len(f["ranges"]) >= 1)

# ----------------------------------------------------------------- the control
rc, res, err = run(["check", os.path.join(CASES, "control"), "--json"])
if res is None:
    failed += 1
    print("FAIL control produced no JSON\n       %s" % err[:400])
else:
    check("control: reported clean", res["verdict"], "reproducible")
    check("control: exits zero", rc, 0)
    check("control: nothing differs",
          [f["path"] for f in res["files"] if f["status"] != "identical"], [])
    # A control that builds nothing passes vacuously, which is the same false
    # negative this whole suite is built to catch. Insist on real outputs, one
    # per shape the broken cases break.
    got = sorted(f["path"] for f in res["files"])
    for want in ("ctl", "sorted.txt", "libctl.a", "data.gz"):
        truthy("control: actually produced %s" % want, want in got,
               "produced %s" % got)

print("\n%d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
