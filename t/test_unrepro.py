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
import shutil
import stat
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

# The guard that stops every text file from looking full of timestamps.
check("epoch_nums rejects printable ASCII", unrepro.epoch_nums(b"Beta", 0, 4), [])
check("epoch_nums rejects a newline-bearing text run",
      unrepro.epoch_nums(b"n\nGa", 0, 4), [])
check("epoch_nums rejects round padding words",
      unrepro.epoch_nums(b"\x00\x00\x00\x40", 0, 4), [])
truthy("epoch_nums accepts a real binary epoch",
       any(v == 1234567890 for _, _, v in
           unrepro.epoch_nums(b"\xd2\x02\x96\x49", 0, 4)))

# ================================================================ unit: formats

GZ_A = b"\x1f\x8b\x08\x00" + (1700000000).to_bytes(4, "little") + b"\x00\x03" + b"PAY"
GZ_B = b"\x1f\x8b\x08\x00" + (1234567890).to_bytes(4, "little") + b"\x00\x03" + b"PAY"
check("gz_header reads MTIME", unrepro.gz_header(GZ_A)["mtime"], 1700000000)
check("gz_header finds the payload offset", unrepro.gz_header(GZ_A)["payload"], 10)
check("gz_header rejects a non-gzip", unrepro.gz_header(b"not a gzip at all"), None)


def ar_member(name, mtime, uid, gid, mode, data):
    h = (name.ljust(16)[:16] + str(mtime).ljust(12) + str(uid).ljust(6) +
         str(gid).ljust(6) + mode.ljust(8) + str(len(data)).ljust(10))
    return h.encode() + b"`\n" + data + (b"\n" if len(data) % 2 else b"")


AR_A = unrepro.AR_MAGIC + ar_member("x.o/", 1700000000, 1000, 1000, "100644", b"OBJ")
AR_B = unrepro.AR_MAGIC + ar_member("x.o/", 1234567890, 0, 0, "100600", b"OBJ")
ms = unrepro.ar_members(AR_A)
check("ar_members finds one member", len(ms), 1)
check("ar_members strips the GNU name terminator", ms[0]["name"], "x.o")
check("ar_members reads mtime", ms[0]["mtime"], "1700000000")
check("ar_members rejects a non-archive", unrepro.ar_members(b"nope"), None)

secs = unrepro.elf_sections(open("/proc/self/exe", "rb").read()
                            if os.path.exists("/proc/self/exe") else b"")
truthy("elf_sections parses a real ELF64 binary", any(n == ".text" for n, *_ in secs),
       "got %d sections" % len(secs))
check("elf_sections declines a non-ELF", unrepro.elf_sections(b"MZ not elf"), [])

# ============================================================ unit: diagnosis

NOCTX = {"paths": [], "hosts": [], "users": [], "epoch_b": None,
         "locale_a": "C", "locale_b": "en_US.UTF-8"}

f = unrepro.diagnose("x.gz", GZ_A, GZ_B, NOCTX)
check("a gzip mtime difference is diagnosed as gzip-mtime", f["cause"], "gzip-mtime")
truthy("and says the payload is identical", "ONLY difference" in alltext(f))

f = unrepro.diagnose("x.a", AR_A, AR_B, NOCTX)
check("an ar header difference is diagnosed as ar-metadata", f["cause"], "ar-metadata")
truthy("and names the fields", "mtime" in alltext(f) and "mode" in alltext(f))

f = unrepro.diagnose("l.txt", b"Beta\nalpha\n", b"alpha\nBeta\n", NOCTX)
check("a permutation is diagnosed as reorder", f["cause"], "reorder")
truthy("and blames collation", "collation" in alltext(f))

f = unrepro.diagnose("p.txt", b"dir=/build/aa\n", b"dir=/build/bb\n",
                     dict(NOCTX, paths=[("/build/aa", "/build/bb")]))
check("an embedded build path is diagnosed as build-path", f["cause"], "build-path")

f = unrepro.diagnose("h.txt", b"host=alpha.example\n", b"host=beta.example\n",
                     dict(NOCTX, hosts=[("alpha.example", "beta.example")]))
check("an embedded hostname is diagnosed as build-host", f["cause"], "build-host")

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
check("--no-vary path holds the directory constant",
      unrepro.variants(["path"])[1]["dirname"], a["dirname"])
LOCALE_OK = b["locale"] != a["locale"]

# best_locale() must pick a locale that COLLATES differently from C, not one
# that is merely spelled differently. Its candidate list ends in "c.utf8",
# which is a non-C locale by name and sorts byte-wise exactly like C -- so on
# a system carrying only C, POSIX and C.UTF-8 it used to return C.UTF-8, the
# ls-order case would run, both builds would sort identically, and ls-order
# would be reported as a FALSE POSITIVE: unrepro accusing a reproducible
# build because its own variation did nothing.
check("the C locale is not accepted as a collation variant",
      unrepro.collates_unlike_c("C"), False)
if any(h.lower().replace("-", "") == "c.utf8"
       for h in subprocess.run(["locale", "-a"], capture_output=True,
                               text=True).stdout.split()):
    check("...and neither is C.UTF-8, which collates byte-wise like C",
          unrepro.collates_unlike_c("C.utf8"), False)
    truthy("...so best_locale() does not return it",
           unrepro.best_locale().lower().replace("-", "") != "c.utf8")
else:
    print("note: no C.UTF-8 on this system, so that half cannot be checked")
truthy("the locale best_locale() picked really does collate differently",
       unrepro.best_locale() == "C"
       or unrepro.collates_unlike_c(unrepro.best_locale()))
# Recorded honestly: on a machine that HAS en_US.UTF-8, reverting
# best_locale() to compare names instead of collation is an EQUIVALENT
# mutant -- en_US.UTF-8 is tried first and does collate differently, so
# c.utf8 is never reached and the selection is unchanged. The bug only
# manifests where C.UTF-8 is the only non-C locale, which is Debian slim and
# many container bases. What the three checks above DO discriminate is the
# collation test itself: making collates_unlike_c always return True turns
# two of them red.

if not LOCALE_OK:
    print("note: only C locales installed; the ls-order case cannot trigger here")

# ============================================= the oracle: planted nondeterminism

# (case directory, output file, required cause, a substring of the evidence)
PLANTED = [
    ("date-macro", "dm", "timestamp", "SOURCE_DATE_EPOCH=1234567890"),
    ("file-macro", "fm", "build-path", "build-a"),
    ("date-cmd", "version.h", "timestamp", ":"),
    ("ls-order", "manifest.txt", "reorder", "permutation"),
    ("ar-stamp", "libstamp.a", "ar-metadata", "mtime"),
    ("gzip-mtime", "data.gz", "gzip-mtime", "MTIME"),
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

# ------------------------------------------- the variations are load-bearing
#
# Each planted case is irreproducible along exactly one axis, so holding that one
# axis constant must make it go clean. This is what proves the variations are
# doing the work rather than decorating the report: if --no-vary time still
# caught the __DATE__ case, something other than the clock would be responsible
# and the diagnosis would be a coincidence.
# date-cmd is deliberately absent: date(1) honours no clock override at all, so
# the only way to pin it is libfaketime, which is not stdlib. Its bisection would
# be decided by whether a second ticked between the two builds, and a flaky
# assertion is worse than a missing one. Said plainly in README and PLAN instead.
# Both lists are hard-coded, and nothing asserted their length or
# cross-checked them against t/cases/ on disk. Replacing them with [] gave
# "69 passed, 0 failed" and exit 0: all 6 planted cases and all 5 bisections
# -- 36 assertions -- vanished and the suite stayed green. "7/7 diagnosed" is
# true today and was enforced by nothing, and an eighth case directory would
# have been silently untested.
#
# The check is against the directory rather than against another literal:
# every case dir except `control` must appear in PLANTED, and `control` is
# used separately below.
CASE_DIRS = sorted(d for d in os.listdir(os.path.join(HERE, "cases"))
                   if os.path.isdir(os.path.join(HERE, "cases", d)))
_planted_names = sorted(c[0] for c in PLANTED)
_expect_planted = sorted(d for d in CASE_DIRS if d != "control")
check("every case directory except control is a planted case",
      _planted_names, _expect_planted)
truthy("...and there are some, so the comparison is not two empty lists",
       len(_expect_planted) >= 5)

BISECT = [
    ("date-macro", "time"),
    ("file-macro", "path"),
    ("gzip-mtime", "time"),
    ("ar-stamp", "time,umask"),   # ar records both the clock and the file mode
    ("ls-order", "locale"),
]
# BISECT is a subset of the planted cases, chosen for having a single
# identifiable axis. Pinned the same way: a list that shrank to nothing used
# to take its assertions with it silently.
truthy("every bisection case is a planted case",
       all(c in _planted_names for c, _ in BISECT))
check("the bisection set is the expected size", len(BISECT), 5)

for case, axis in BISECT:
    if case == "ls-order" and not LOCALE_OK:
        # Said out loud, like its twin in the PLANTED loop above. This was a
        # bare `continue`, so on a machine with no non-C locale the
        # bisection for ls-order disappeared with no line and no counter.
        print("skip %s bisection (no non-C locale available)" % case)
        continue
    rc, res, err = run(["check", os.path.join(CASES, case), "--no-vary", axis,
                        "--json"])
    check("%s goes clean with --no-vary %s" % (case, axis),
          res["verdict"] if res else err, "reproducible")

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

# ------------------------- a tree that already holds its products is judgeable
# The bug this guards against was found in the wild, on a sibling repo whose
# binary happened to be sitting in the working directory. A perfectly
# reproducible build rewrites its products to the bytes that were already
# there, so hash-only output detection sees nothing change -- and the tool
# answered "inconclusive, no outputs observed" on the cleanest result it can
# ever be handed, while the compile line sat in the log it printed.
with tempfile.TemporaryDirectory() as tmp:
    pre = os.path.join(tmp, "prebuilt")
    shutil.copytree(os.path.join(CASES, "control"), pre)
    subprocess.run(["make", "-C", pre], capture_output=True, timeout=600)
    truthy("prebuilt: the fixture really is prebuilt",
           os.path.exists(os.path.join(pre, "ctl")))

    rc, res, err = run(["check", pre, "--cmd", "make -B", "--json"])
    if res is None:
        failed += 1
        print("FAIL prebuilt produced no JSON\n       %s" % err[:400])
    else:
        check("prebuilt: a forced rebuild gets a verdict",
              res["verdict"], "reproducible")
        check("prebuilt: exits zero", rc, 0)
        # Not just "a verdict": the products themselves have to be in it, or
        # the verdict is about some incidental file and proves nothing.
        got = sorted(f["path"] for f in res["files"])
        for want in ("ctl", "libctl.a", "data.gz"):
            truthy("prebuilt: compared %s" % want, want in got,
                   "compared %s" % got)

    # The guard against a false REPRODUCIBLE has to survive the fix. A build
    # that genuinely writes nothing is still inconclusive, and the distinction
    # between the two is the whole point of this pair.
    rc, res, err = run(["check", pre, "--json"])
    check("prebuilt: an unforced no-op is still inconclusive",
          res["verdict"] if res else err, "inconclusive")
    check("prebuilt: and still exits 2", rc, 2)

# A rule that rewrites its output and then puts the timestamp back defeats
# mtime. ctime does not care, and no unprivileged caller can set it.
with tempfile.TemporaryDirectory() as tmp:
    with open(os.path.join(tmp, "src.txt"), "w") as fh:
        fh.write("payload\n")
    with open(os.path.join(tmp, "Makefile"), "w") as fh:
        fh.write("all:\n"
                 "\tcp -p src.txt out.txt\n"
                 "\ttouch -r src.txt out.txt\n")
    # Run it once so out.txt exists with src.txt's mtime already. The build
    # under test then rewrites it to the same bytes at the same mtime, which
    # leaves ctime as the only thing that moved.
    subprocess.run(["make", "-C", tmp], capture_output=True, timeout=120)
    rc, res, err = run(["check", tmp, "--cmd", "make -B", "--json"])
    truthy("mtime-restoring rule: output still compared",
           res is not None and "out.txt" in [f["path"] for f in res["files"]],
           "got %s" % (res["files"] if res else err[:200]))

# A source tree holding hostile symlinks must still be checkable. copytree
# follows links by default, which turns a dangling link into a raised
# shutil.Error and a link loop into ELOOP -- reported as an unhandled
# traceback, with no verdict at all. Found on tarx, whose test tree keeps a
# dangling link, an over-long one and a 42-level loop on purpose, because
# refusing to follow a hostile symlink is what tarx is for.
with tempfile.TemporaryDirectory() as tmp:
    with open(os.path.join(tmp, "Makefile"), "w") as fh:
        fh.write("all:\n\t$(CC) -O2 -o prog prog.c\n")
    with open(os.path.join(tmp, "prog.c"), "w") as fh:
        fh.write("int main(void){return 0;}\n")
    os.symlink("nowhere-at-all", os.path.join(tmp, "dangling"))
    os.mkdir(os.path.join(tmp, "d"))
    os.symlink("..", os.path.join(tmp, "d", "loop"))
    os.symlink("d", os.path.join(tmp, "to_dir"))

    rc, res, err = run(["check", tmp, "--cmd", "make -B", "--json"])
    truthy("a tree with dangling and looping symlinks still gets a verdict",
           res is not None and res["verdict"] in ("reproducible",
                                                  "not-reproducible"),
           "rc=%s %s" % (rc, (err or "")[:300]))
    truthy("and the build inside it actually produced something",
           res is not None and "prog" in [f["path"] for f in res["files"]],
           "compared %s" % ([f["path"] for f in res["files"]] if res else err[:200]))

# ------------------------------------- the invariant: never clean on no evidence
with tempfile.TemporaryDirectory() as tmp:
    with open(os.path.join(tmp, "Makefile"), "w") as fh:
        fh.write("all:\n\t@true\n")
    rc, res, err = run(["check", tmp, "--json"])
    check("a build that produces nothing is inconclusive, not reproducible",
          res["verdict"] if res else err, "inconclusive")
    check("and exits nonzero", rc, 2)

# ---------------------------------------------------------- the diff subcommand
with tempfile.TemporaryDirectory() as tmp:
    pa, pb = os.path.join(tmp, "a.gz"), os.path.join(tmp, "b.gz")
    open(pa, "wb").write(GZ_A)
    open(pb, "wb").write(GZ_B)
    rc, res, err = run(["diff", pa, pb, "--json"])
    check("diff on two files exits 1", rc, 1)
    check("diff diagnoses the gzip header",
          res["files"][0]["cause"] if res else err, "gzip-mtime")
    rc, res, err = run(["diff", pa, pa, "--json"])
    check("diff on identical files exits 0", rc, 0)
    check("diff on identical files says reproducible",
          res["verdict"] if res else err, "reproducible")

# ================================================ against a real sibling build

# Informational, not pass/fail on the verdict: whether a neighbouring repo is
# reproducible is not something this suite controls, and pinning it would make
# the suite fail for the wrong reason the moment someone fixed their CFLAGS.
# What IS asserted is that unrepro survives a real build and returns a verdict.
REAL = None
for cand in ("cc", "walkv", "rx", "trace"):
    p = os.path.join(os.path.dirname(ROOT), cand)
    if os.path.isfile(os.path.join(p, "Makefile")):
        REAL = p
        break
if REAL and not os.environ.get("UNREPRO_SKIP_REAL"):
    # -B because these trees carry their built binaries, which would otherwise
    # make `make` a no-op -- exactly the condition the inconclusive guard exists
    # to refuse.
    rc, res, err = run(["check", REAL, "--cmd", "make -B", "--json"])
    truthy("real build (%s): returns a verdict" % os.path.basename(REAL),
           res is not None and res["verdict"] in
           ("reproducible", "not-reproducible", "inconclusive"),
           (err or "")[:300])
    if res:
        print("     real finding: %s -> %s" % (os.path.basename(REAL), res["verdict"]))
        for f in res["files"]:
            if f["status"] != "identical":
                print("       %s: %s" % (f["path"], f["cause"]))
                for line in f["detail"][:4]:
                    print("         " + line)
else:
    print("skip real sibling build (none found, or UNREPRO_SKIP_REAL set)")

# ------------------------------------------------ a fifo in the source tree
#
# shutil.copytree cannot copy a named pipe: it raises shutil.Error with "is a
# named pipe", which is not the vanished-file case the handler covers, so the
# traceback escaped and `unrepro check` died instead of returning a verdict.
#
# This was not hypothetical. httpd's suite leaves a fifo in t/tmp/root/ from
# the test asserting that a fifo is not a document, so pointing unrepro at
# httpd printed a Python stack trace. Any tree with a fifo anywhere in it did.
#
# Skipping is the right answer rather than a workaround: a build output is a
# regular file, and the contents of a fifo cannot be compared between two
# builds even in principle. The skip is reported, because a silent one would
# hide a tree that is mostly unreadable.
with tempfile.TemporaryDirectory() as td:
    proj = os.path.join(td, "fifoproj")
    os.makedirs(os.path.join(proj, "sub"))
    with open(os.path.join(proj, "main.c"), "w") as fh:
        fh.write("int main(void){return 0;}\n")
    with open(os.path.join(proj, "Makefile"), "w") as fh:
        fh.write("all: prog\nprog: main.c\n\t$(CC) -O0 -o $@ $<\n")
    os.mkfifo(os.path.join(proj, "sub", "apipe"))

    rc, res, err = run(["check", proj, "--cmd", "make -B"])
    truthy("a fifo in the tree does not crash the tool",
           "Traceback" not in err, err[-300:])
    check("...and a verdict is still produced", rc, 0)
    truthy("...and the skip is reported rather than silent",
           "non-regular file" in err, err[-300:])
    truthy("...and it names the fifo it skipped", "apipe" in err, err[-300:])
    # The fixture has to actually contain one, or the three above pass for the
    # wrong reason.
    truthy("the fixture really did contain a fifo",
           stat.S_ISFIFO(os.lstat(os.path.join(proj, "sub", "apipe")).st_mode))

print("\n%d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
