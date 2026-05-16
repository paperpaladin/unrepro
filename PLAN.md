# unrepro -- plan

## Contract

`unrepro check DIR` copies DIR into two fresh build directories, builds it twice
under deliberately different conditions, compares every file the build created or
changed, and for each differing file names the mechanism that caused it.
`unrepro diff A B` does the comparison and diagnosis half against two files or
two trees that already exist. `--json` on either.

Exit status: 0 reproducible, 1 differences found, 2 error -- build failure, bad
usage, or **inconclusive**, which is its own outcome and never collapses into 0.

Nothing is installed, nothing leaves the machine, and the only non-stdlib things
touched are the build's own tools (`make`, `gcc`, `ar`, `gzip`) inside the
temporary trees.

## Hard invariant

**Never report clean on a build that is not reproducible.** False negatives are
the dangerous direction: a tool that prints reassurance is the thing that stops
anyone else from looking, and the build stays broken for years. Two consequences
are enforced in code:

- Two builds in the same directory, at the same instant, with the same
  environment, will match even when the build is hopelessly unreproducible, so a
  clean result from two *identical* builds proves nothing. Every axis is varied
  by default, and `t/test_unrepro.py` asserts that holding any one axis constant
  makes the case planted on that axis go clean -- which is how we know the
  variations are load-bearing and not decorative.
- Zero observed outputs is `inconclusive`, exit 2 -- never `reproducible`. A tree
  that already contains its build products makes `make` a no-op, and "0 files
  compared, 0 differ" read as success is the purest form of this bug. The first
  real repo pointed at this tool hit exactly that.

**Second invariant: every diagnosis names a cause that is actually present.** Not
"differs", and not a guess. A `timestamp` verdict has to exhibit bytes that parse
as a date; a `build-path` verdict has to exhibit the build directory string found
in the artifact; an `ar-metadata` verdict has to name the header field and show
both values. When nothing matches, the verdict is `unknown` with a byte offset
and a hex window -- an honest "I found it but cannot explain it" rather than the
nearest plausible-sounding cause. Three guards exist only to protect this
invariant, each one added after the tool was caught violating it:

- printable-ASCII runs are not timestamps (`b"Beta"` unpacks to 2021-10-23),
- words with two or more zero bytes are padding, not clocks (`0x40000000`),
- once anything changes length, every later offset is shifted, so binary
  timestamp sniffing is suppressed -- it invented dates out of relocated DWARF on
  four real repositories before that rule existed.

## Oracle

Ranked by how much each would hurt to lose.

1. `t/cases/` -- seven tiny projects, six irreproducible in exactly ONE known way
   and one correct. Every broken case asserts **both** directions: the verdict is
   not-clean, *and* the cause equals the specific planted mechanism, *and* the
   evidence text contains the concrete value. `date-macro` must say `timestamp`
   and quote the `SOURCE_DATE_EPOCH` it was handed; `ar-stamp` must say
   `ar-metadata` and name `mtime`. A tool that says "differs" without saying why
   is not useful, so detection alone is not a passing grade.
2. The control case, and the assertion that it actually produced its four
   outputs. A control that compiles nothing passes vacuously, which is the same
   false negative wearing a different hat. Crying wolf is the other way to die:
   a checker that flags a correct build gets switched off, after which it catches
   nothing.
3. The bisection matrix -- `--no-vary AXIS` must make the case planted on that
   axis go clean. This is what proves the variations cause the detections.
4. A real build. `make test` runs unrepro against a sibling C repo and asserts
   only that it returns a verdict, because whether someone else's `CFLAGS` are
   reproducible is not this suite's business to pin.
5. Unit tests on the format parsers and the false-positive guards, with bytes
   constructed in the test rather than built, so a parser regression is located
   in one line instead of somewhere inside a two-build run.

## Non-goals

Being diffoscope. No recursive unpacking of nested containers, no decompilers, no
format plugins, no HTML report. Not a build system, not a packaging tool, not a
patch generator. No attempt to compare file permissions, ownership or mtimes --
only bytes, because the umask is varied on purpose and metadata differences are
the packaging layer's problem. No daemon, no server, no database.

## Commit sequence

Twenty-seven commits, each one leaving `make test` green. Grouped here rather
than listed one per line; `git log --oneline` has the detail.

- [x] 00 the two-build driver, tree comparison, the honest `unknown` fallback
      with a byte offset and a hex window, the CLI and `--json`
      why: the varying is the whole product. Everything else is a byte compare,
           which is one line and proves nothing on its own.
- [x] 01 the harness, and `t/cases/control` -- the case that must report clean
      why: the control is the canary for every variation added after it. If one
           of them makes a correct build look broken, this is what notices.
- [x] 02 one commit per variation axis: path, time, locale, umask, host/user, -j
      why: six separate commits because the axes are independent and each one is
           separately revertable. `git log -S` on an axis name now finds the
           commit that introduced it and the reasoning for its values.
- [x] 03 the ELF section table parser, used both to locate a difference and as a
      last-resort cause when section sizes differ
      why: "0x1ddac" is not a finding; "0x1ddac, section .debug_line_str" is.
- [x] 04 five planted broken cases, each committed RED then GREEN: the case
      first, asserting whatever wrong or vague verdict the tool actually gives,
      then the diagnoser that fixes it
      why: the red commits are the argument for the whole project. file-macro
           lands reporting five fabricated dates in 1997, 2014 and 2023 out of
           relocated machine code, and ar-stamp lands reporting a "conclusive"
           timestamp without mentioning that it is an archive header field.
           Both are committed as asserted, wrong behaviour so that the diff to
           the fix is the evidence.
- [x] 05 three guards against naming a cause that is not present: the text-run
      guard, the round-number guard, and suppression under a layout shift
      why: each one was the second invariant being violated by the tool, found
           by the planted cases and by four real repositories.
- [x] 06 the inconclusive verdict for a build that produced no outputs
      why: found by pointing the tool at four sibling repos, where it said
           "0 compared, 0 differ, REPRODUCIBLE". The worst possible bug here.
- [x] 07 a real sibling build inside `make test`
      why: seven planted cases prove the diagnoses are right on bugs we wrote.
           They prove nothing about bugs we did not.
- [x] 08 `--no-vary`, with the clock PINNED rather than freed when the time axis
      is held constant, and the bisection matrix in the suite
      why: the first version let both builds read the wall clock and was clean
           about nine times in ten. A flaky bisection teaches you to rerun until
           you like the answer.
- [x] 09 `unrepro diff A B`
      why: a finding has to be reproducible by a second person without them
           having to run the build.

### Milestone 1

- [x] 00 distinguish "make was a no-op" from "the build reproduced an artifact
      that was already in the tree"
      why: found while fixing the four sibling repos. After -ffile-prefix-map
           landed, `check` reported "the build produced no new or changed files"
           even though the compile line is right there in the output -- because
           the rebuild was now byte-identical to the binary already sitting in
           the working tree. The inconclusive guard, which exists to prevent a
           false REPRODUCIBLE, now also fires on a *perfectly* reproducible
           build. Requiring a clean tree is the workaround; hashing before the
           first build and comparing to it is the fix.
      done: an output is any file the build rewrote -- content hash, mtime or
           ctime -- rather than any file whose content changed. ctime is in
           there because mtime can be restored and ctime cannot be set at all
           by an unprivileged caller, which closes the one hole mtime alone
           leaves. Hit again in the wild on `jail` the day before the fix,
           which is where the reproducer came from. Three assertions, and the
           no-op case is still inconclusive -- that pair is the whole point.
 -- the variations that are still missing

- [ ] 04 shuffled `readdir` order via an `LD_PRELOAD` shim wrapping
      `readdir`/`readdir64`, buffering each `DIR*`'s entries and returning them
      reversed
      why: the single biggest gap. Any build that globs, or walks a directory
           without sorting, is order-dependent, and on ext4 the order is stable
           per-directory -- so this bug is invisible on the machine that has it
           and explodes on the buildd. `ls`-based lists are caught today only
           because the *locale* changed, which is the wrong reason to be right.
- [ ] 05 pin the wall clock with an `LD_PRELOAD` shim over `time`/`gettimeofday`/
      `clock_gettime`, offsetting it by months between the two builds
      why: `date(1)`, `ar` without `U`, and anything calling `time()` ignore
           `SOURCE_DATE_EPOCH`, so today the `date-cmd` case is detected only
           because `TZ` changed and its bisection cannot be asserted at all.
- [ ] 06 vary the number of CPUs visible to the build (`taskset`) and the kernel
      `uname -m`/`-r` strings
      why: `-j` alone does not reproduce the "builds differently on a 96-core
           machine" class of bug; thread-count-dependent output does.
- [ ] 07 vary the user's primary group and the filesystem (tmpfs vs ext4), so
      `tar`/`cpio` metadata and inode-order effects show up
      why: two of the Debian taxonomy's biggest buckets live here.
- [ ] 08 `tar` and `cpio` member diagnosis: order, mtime, uid/gid/uname/gname,
      mode, and the `pax` header
      why: `.tar.gz` is the shape most source releases ship in, and today
           unrepro diagnoses the gzip wrapper and then gives up on the contents.
- [ ] 09 recurse into archives: diff `.a` and `.tar.gz` members as files, so a
      verdict reads `libfoo.a(bar.o) .comment` rather than `libfoo.a`
      why: the current archive diagnosis stops at the member headers, which is
           exactly where the interesting half starts.

### Milestone 2 -- a real campaign

- [ ] 10 run unrepro against 20 real Debian and Arch source packages, offline
      from a local mirror, and record every verdict verbatim including the
      `unknown` ones
      why: seven planted cases prove the diagnoses are correct; they prove
           nothing about coverage. The honest measure of this tool is what
           fraction of real failures it can name, and the only way to learn that
           is to be wrong in public twenty times.
- [ ] 11 build a taxonomy of causes from that campaign -- count per cause, and a
      ranked list of the `unknown` buckets by frequency
      why: decides what to implement next on evidence instead of on which
           diagnoser is most fun to write.
- [ ] 12 cross-check the taxonomy against the Reproducible Builds project's own
      issue classification, and adopt their names where they already have one
      why: a second vocabulary for the same bugs makes every report harder to
           read and harder to merge upstream.
- [ ] 13 submit the fixes upstream, one patch per package, each citing the
      byte offset and the varied axis that exposed it
      why: a checker whose findings never become patches is a scoreboard. The
           output is designed to be pasteable into a bug report for this reason.
- [ ] 14 publish the `unknown` cases as reproducers, with the exact two build
      directories and the hex windows
      why: the ones we cannot explain are the most valuable thing in the
           campaign and the easiest to lose.

### Milestone 3

- [ ] 15 a `--bisect` flag that attributes each difference to an axis by itself:
      on a difference, rebuild holding each axis constant in turn and report
      "caused by: time"
      why: `--no-vary` already does this, by hand, one axis at a time. Every
           report in this README was produced by running it six times and
           reading the verdicts, which is exactly the kind of loop a machine
           should be doing.

## Open questions

- Varying `SOURCE_DATE_EPOCH` between the builds is not what `reprotest` does,
  and it has a real cost: a build that *correctly* honours it is reported as
  differing. That is defensible -- the diagnosis says so explicitly, in the
  "conclusive" line -- but it means `unrepro check` on a well-behaved,
  `SOURCE_DATE_EPOCH`-respecting package is noisy. The alternative is to pass the
  same epoch to both and vary only the wall clock, which needs item 05 first.
- Outputs are identified as "files whose content changed during the build". That
  misses a build which writes outside its own tree, and it counts intermediate
  files like `.o` as outputs. Counting `.o` has turned out to be a feature -- in
  `ar-stamp` it is what proves the archive headers are the only problem -- but it
  is still an accident rather than a decision.
- `--no-vary path` moves build A's tree aside and builds B at the identical path,
  which is correct but means the two builds are not symmetric with the default
  case. Unclear whether that ever matters.
- File permissions are never compared, deliberately, because the umask is varied.
  But a build whose *output* mode depends on the umask is a real bug, and right
  now nothing would catch it.
