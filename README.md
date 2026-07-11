# unrepro

Builds a project twice under deliberately different conditions and, when the
outputs differ, names the specific source of nondeterminism instead of reporting
"files differ".

```
$ unrepro check t/cases/ar-stamp
unrepro: /home/x/projects/unrepro/t/cases/ar-stamp
  build A  /tmp/unrepro.ih9du604/build-a  TZ=UTC  LC_ALL=C  umask=022  -j1  HOSTNAME=builder-one  USER=alice  SOURCE_DATE_EPOCH=unset
  build B  /tmp/unrepro.ih9du604/b-second-build-dir-with-a-much-longer-name  TZ=Asia/Tokyo  LC_ALL=en_US.utf8  umask=077  -j4  HOSTNAME=buildd-42.example.org  USER=bob  SOURCE_DATE_EPOCH=1234567890

3 output file(s) compared, 1 differ

  a.o                          ok
  b.o                          ok
  libstamp.a                   DIFFERS   ar-metadata
      ar member header field(s) differ: mode, mtime
        member a.o              mtime  A=1000000000   B=1234567890     (2001-09-09T01:46:40Z vs 2009-02-13T23:31:30Z)
        member a.o              mode   A=100644       B=100600
        member b.o              mtime  A=1000000000   B=1234567890     (2001-09-09T01:46:40Z vs 2009-02-13T23:31:30Z)
        member b.o              mode   A=100644       B=100600
      member payloads are identical
      fix: ar D (deterministic: mtime/uid/gid zeroed, mode normalised)

VERDICT: NOT REPRODUCIBLE -- 1 of 3 output(s) differ: ar-metadata
```

(Both `A` mtimes used to be a wall-clock value, which is why this paragraph
used to quote `1791268131` while the transcript above read `1000000000`. They
come from `SOURCE_DATE_EPOCH` now, so the numbers above are the numbers you
will get; the reason is a flake worth reading about -- see *The suite was
non-deterministic* below.)

(That line read `1 of 1` until an audit noticed it contradicts the
`3 output file(s) compared, 1 differ` eight lines above it in the same
block. `unrepro.py` prints `len(diff)` of `len(res["files"])`, so the two
numbers cannot both come from one run -- the sample had been edited by
hand, which makes a transcript evidence for nothing.)

The `.o` files are byte-identical and the archive members are byte-identical; the
only thing wrong is the 60-byte header in front of each one. "libstamp.a differs"
would have told you nothing. `member a.o mtime A=1000000000 B=1234567890`
tells you to add `D`.

## The variations

Two back-to-back builds in the same directory, at the same instant, in the same
environment will usually match even when the build is hopelessly unreproducible.
**A clean result from two identical builds proves nothing at all.** The entire
value of this tool is in what it changes between build 1 and build 2:

| axis | build A | build B | catches |
|---|---|---|---|
| `path` | `build-a` | `b-second-build-dir-with-a-much-longer-name` | `__FILE__`, `getcwd`, DWARF `DW_AT_comp_dir`, `-I` prefixes |
| `time` | `TZ=UTC`, no `SOURCE_DATE_EPOCH` | `TZ=Asia/Tokyo`, `SOURCE_DATE_EPOCH=1234567890` | `__DATE__`/`__TIME__`, `date >> version.h`, archive and gzip mtimes |
| `locale` | `LC_ALL=C` | `LC_ALL=en_US.UTF-8` | file lists built with `ls` or `sort`, whose collation is locale-dependent |
| `umask` | `022` | `077` | modes recorded inside `ar` and `tar` members |
| `host` | `builder-one` / `alice` | `buildd-42.example.org` / `bob` | build-host and build-user identity baked into artifacts |
| `jobs` | `-j1` | `-j4` | archive member order, link order, anything racing |

Varying the build **path** matters as much as varying the clock. The clock class
of bug is the famous one, but path leakage is at least as common and far harder to
notice: anything that compiles with an absolute source path ships the identity of
one machine's directory layout inside the artifact, and two builds in the same
directory can never show you that. It is also what every sibling repository in
this tree turned out to be guilty of -- see "A real finding" below. The second
directory name is a different *length* on purpose, so a leaked path shifts every
later offset in the file, which is a much louder signal than a same-length
substitution.

`--no-vary AXIS[,AXIS]` holds an axis constant, which is how you attribute a
difference to one cause:

```
$ unrepro check t/cases/file-macro --no-vary path
VERDICT: REPRODUCIBLE across all varied conditions (1 output(s))
$ unrepro check t/cases/file-macro --no-vary time
VERDICT: NOT REPRODUCIBLE -- 1 of 1 output(s) differ: build-path
```

## The diagnosis

Each differing file gets one verdict, the supporting evidence, and a suggested
fix. Diagnosers are tried in order of how specifically they name a mechanism, and
every match that is not the primary one is kept as `also:`, because a file can
genuinely have two bugs.

| cause | what it means |
|---|---|
| `gzip-mtime` | the 4-byte MTIME at offset 4 differs; both decoded, and whether the deflate payload behind it is identical |
| `ar-order` | the archive's members are the same set in a different sequence |
| `ar-metadata` | member `mtime`/`uid`/`gid`/`mode` differ; which field, both values, both dates |
| `build-path` | the build directory string was found inside the artifact, at an offset, in a named ELF section |
| `build-host` | ditto for the hostname or user name |
| `reorder` | the two outputs are a permutation: identical byte multiset, identical line set, different order |
| `timestamp` | the differing bytes parse as a date -- as text, or as a 4-byte little-endian epoch -- and if they match the `SOURCE_DATE_EPOCH` we handed build B, the report says so and calls it conclusive |
| `elf-sections` | section sizes differ, so the difference is a layout shift rather than an in-place swap; names the section that moved |
| `unknown` | nothing matched. Reports the byte offset, the containing ELF section, and a hex window for both sides |

ELF files are located before being classified: the section table is parsed
directly (thirty lines of `struct`, rather than parsing `readelf`'s column layout,
which overflows on long section names) so a verdict can say *where* in the binary
the difference is. Files up to 4 KiB get a full byte-offset diff:

```
  data.gz                      DIFFERS   gzip-mtime
      gzip header MTIME (offset 0x04..0x07) differs:
        A 1791268131  (2026-10-06T06:28:51Z)   <- wall clock; yours will differ
        B 1234567890  (2009-02-13T23:31:30Z)
      deflate payload after the header is byte-identical, so the header is the ONLY difference
      fix: gzip -n (writes MTIME=0 into the header)
      byte-offset diff:
      @ 0x4..0x8 (4 bytes)
        A 00000000  1f 8b 08 08 23 95 c4 6a 00 03 64 61 74 61 2e 74  ....#..j..data.t
        B 00000000  1f 8b 08 08 d2 02 96 49 00 03 64 61 74 61 2e 74  .......I..data.t
```

`unrepro diff A B` runs just the comparison and diagnosis against two files or
two trees that already exist, for when the builds happened somewhere else.
`--json` on either subcommand emits the whole structure, byte ranges included.

## Why this exists

Reproducible builds are how you detect the xz class of attack. The xz backdoor
lived in the release tarball and not in the git tree, so anyone who could compare
"what the published artifact is" against "what the source says it should be" would
have seen it -- but that comparison is only possible if building the source twice
gives you the same bytes. Until it does, every artifact is unfalsifiable, and
"trust the maintainer" is the whole security model.

Getting there is unglamorous, and the [Reproducible Builds](https://reproducible-builds.org/)
project is a small, active community doing it one package at a time: a shared
taxonomy of causes, `SOURCE_DATE_EPOCH` as a convention, patches upstreamed for
years. `diffoscope` is their comparison tool and is vastly more capable than this
one. unrepro is the small, dependency-free, offline version of the same idea, with
one thing it insists on: the *varying* is the product. A checker that builds twice
the same way is a checker that reports clean.

## The oracle

`t/cases/` holds seven tiny self-contained projects. Six are irreproducible in
exactly **one** known way; the seventh is correct.

| case | planted cause | required verdict |
|---|---|---|
| `date-macro` | `__DATE__`/`__TIME__` in `.rodata` | `timestamp`, quoting the epoch |
| `file-macro` | `__FILE__` compiled from `$(CURDIR)` | `build-path`, quoting the directory |
| `date-cmd` | `date(1)` output written into a generated header | `timestamp` |
| `ls-order` | file list from `ls`, locale-dependent collation | `reorder` |
| `ar-stamp` | `ar rcsU`: member mtime and mode | `ar-metadata`, naming `mtime` |
| `gzip-mtime` | `gzip` without `-n` | `gzip-mtime`, naming `MTIME` |
| `control` | nothing -- all four shapes done right | **clean** |

Every broken case asserts **both** directions, because they fail differently and
both are bad. A tool that says "differs" without saying why wastes a day of
someone's time, so detection alone is not a passing grade: the cause field must
equal the planted mechanism and the evidence text must contain the concrete value.
A tool that says "clean" on a broken build is worse than nothing, because it is
the reassurance that stops anyone looking -- so that is asserted too, as is the
reverse: the control must come back clean, since a checker that cries wolf on a
correct build gets switched off, after which it catches nothing.

The control also has to prove it *built* something. A control that compiles
nothing passes vacuously, which is the same false negative wearing a different
hat, so the suite insists on all four of its outputs by name.

Then the bisection matrix: `--no-vary AXIS` must make the case planted on that
axis go clean. That is what proves the variations cause the detections rather
than decorating the report.

Writing those cases found five real bugs in unrepro itself, all of the same
shape -- the tool naming a cause that was not there:

- `b"Beta"` unpacks to 1635018050, which is 2021-10-23, so `ls-order`'s plain
  text manifest was confidently diagnosed as a clock bug. Printable-ASCII runs
  are no longer timestamp candidates.
- `0x40000000` is 2004-01-10. Shifted machine code and padding are full of round
  numbers like that, so a candidate with two or more zero bytes in its low word
  is now rejected.
- once anything changes length, "the integer at offset X" compares two unrelated
  things; binary timestamp sniffing is suppressed under a layout shift. It had
  been inventing dates out of relocated DWARF on four real repositories.
- both builds run on the same day, so the date regex matched `Tue Oct  6` on both
  sides and the report quoted the *shared* part as the finding. It now prefers a
  match that actually differs.
- the big one, below.

### A real finding

Pointed at the four sibling C projects in this tree -- `cc`, `walkv`, `rx`,
`trace`, all plain C with a `Makefile` -- unrepro first said this:

```
0 output file(s) compared, 0 differ
VERDICT: REPRODUCIBLE across all varied conditions (0 output(s))
```

Which was a lie, and exactly the lie this tool exists to prevent. All four trees
already contain their built binaries, so `make` was a no-op, nothing was compared,
and "0 differ" was being read as success. Zero observed outputs is now
`inconclusive` and exit 2, never `reproducible`, and it says what to do about it.

That guard then started firing on the opposite case, which took longer to spot
because the output looks the same. A *perfectly* reproducible build rewrites its
products to the bytes already sitting in the tree, so "has anything changed"
answers no and the tool declined to compare the outputs it had just rebuilt --
with the compile line printed in the log right underneath. An output is now any
file the build **rewrote**, judged by content hash, mtime or ctime, so a forced
rebuild over a prebuilt tree gets a real verdict and a build that genuinely does
nothing is still `inconclusive`. ctime is in there because mtime can be put back:
a rule that rewrites its output and then restores the timestamp is invisible to
mtime and not to ctime, which no unprivileged caller can set.

With `--cmd 'make -B'`, the honest answer, for all four:

```
$ unrepro check /home/x/projects/cc --cmd 'make -B'
  cc                           DIFFERS   build-path
      the build directory path is embedded in the output:
        A contains '/tmp/unrepro.s9t76krh/build-a' at offset 0x1ddac (section .debug_line_str)
        B contains '/tmp/unrepro.s9t76krh/b-second-build-dir-with-a-much-longer-name' at offset 0x1ddac (section .debug_line_str)
      the two builds differ only in where they ran, so this string is machine identity, not source
      fix: compile with a relative source path, or -ffile-prefix-map=
      also: elf-sections
          section .debug_line_str      size A=300 B=335

VERDICT: NOT REPRODUCIBLE -- 1 of 1 output(s) differ: build-path
```

`cc`, `walkv`, `rx` and `trace` are all **not reproducible**, all for the same
reason: they build with `-g`, and DWARF's `.debug_line_str` records the directory
the compiler ran in. `.debug_line_str` grew by exactly 35 bytes, which is exactly
how much longer build B's directory name is. The fix for all four is
`-ffile-prefix-map=$(CURDIR)=.` in `CFLAGS`. This is the mundane, boring,
overwhelmingly most common reproducibility bug there is, and no amount of building
twice in the same directory would ever have shown it.

## Status

Works, with the limits below stated plainly.

**What does not work, honestly:**

- **Shuffled `readdir` order is not implemented.** This is the biggest gap. Any
  build that globs or walks a directory without sorting is order-dependent, and on
  ext4 the order is stable per directory -- so the bug is invisible on the machine
  that has it and explodes on someone else's. The `ls-order` case is caught today
  only because the *locale* changed, which is the right answer for the wrong
  reason. Doing it properly needs an `LD_PRELOAD` shim over `readdir`/`readdir64`
  that buffers each `DIR*` and reverses it; that is `PLAN.md` item 04.
- **The wall clock cannot be pinned.** `SOURCE_DATE_EPOCH` is the only clock
  override available without `libfaketime`, which is not stdlib, and `date(1)`,
  `ar` without `U`, and anything calling `time()` ignore it. Consequence: the
  `date-cmd` case is detected only because `TZ` changed, and its `--no-vary time`
  bisection is deliberately not asserted because the answer depends on whether a
  second ticked between the two builds. A flaky assertion is worse than a missing
  one. Item 05.
- **Varying `SOURCE_DATE_EPOCH` is not what `reprotest` does**, and it has a
  cost: a build that *correctly* honours it is reported as differing. The
  diagnosis says so explicitly, but `check` on a well-behaved package is noisy.
- **`tar` and `cpio` are not diagnosed.** A `.tar.gz` gets its gzip wrapper
  explained and then nothing about the contents, which is where the interesting
  half is. Items 08 and 09.
- **No recursion into archives.** A verdict is `libfoo.a`, never
  `libfoo.a(bar.o) .comment`.
- **ELF parsing is 64-bit little-endian only.** Other ELF classes fall back to
  plain byte diffing, which still gives a correct verdict, just an unlocated one.
- **File permissions, ownership and mtimes are never compared**, only bytes --
  deliberately, since the umask is varied. But a build whose output *mode* depends
  on the umask is a real bug and nothing here would catch it.
- **The locale axis silently goes inert** on a system with only `C` and `POSIX`
  installed. It prints a `note:` saying so rather than pretending.
- **Outputs are "files whose content changed during the build"**, so a build that
  writes outside its own tree is missed, and intermediate `.o` files count as
  outputs. The latter has turned out useful -- in `ar-stamp` it is what proves the
  archive headers are the only problem -- but it was an accident, not a decision.
- Only one real-world codebase family has been tried: four small C projects. The
  20-package campaign that would actually measure coverage is Milestone 2.

## Design

One file, `unrepro.py`, about 985 lines, Python 3 stdlib only. No diffoscope, no
`requests`, no third-party anything, no network, nothing installed. The build's
own tools (`make`, `gcc`, `ar`, `gzip`) run inside the temporary trees; `readelf`
is deliberately *not* used, because parsing the ELF section table directly is
thirty lines of `struct` and does not depend on another program's output format.

`check` copies the source tree twice (skipping `.git`, keeping symlinks as
symlinks -- following them made the one sibling whose test tree deliberately
contains a dangling link and a 42-level loop the one repo that could not be
checked), snapshots each copy,
builds with a scrubbed environment plus one variant's knobs, re-snapshots, and
treats every file the build rewrote as an output -- content hash, mtime or ctime,
because the first alone cannot see a reproducible rebuild happen. `umask` is set in the child between
fork and exec via `preexec_fn`, the only stdlib hook for it. `--no-vary path`
moves build A's tree aside and builds B at the identical path, because faking it
with two same-length names would leave a one-character difference that still
leaks.

Diagnosis is a flat list of independent checks over the two byte strings plus a
context dict holding what we know we varied -- the two directory paths, the two
hostnames, the epoch we handed build B. That context is why the verdicts can be
specific rather than suggestive: "these bytes look like a date" is a guess, while
"these bytes are the `__DATE__` rendering of the `SOURCE_DATE_EPOCH` we supplied"
is a proof. `diff` runs the same diagnosers with a thinner context -- the two tree
roots, no known epoch -- and the report says what that costs.

There is no plugin system, no abstract `Diagnoser` base class, and no format
registry. There are nine diagnosers and they are nine `if` blocks; the second use
case will decide whether that needs to change.

## Tests

```
$ make test
...
ok   date-macro: diagnosed as timestamp
ok   date-macro: evidence mentions 'SOURCE_DATE_EPOCH=1234567890'
ok   file-macro: diagnosed as build-path
ok   ls-order: diagnosed as reorder
ok   ar-stamp: diagnosed as ar-metadata
ok   gzip-mtime: diagnosed as gzip-mtime
ok   control: reported clean
ok   control: actually produced libctl.a
ok   prebuilt: a forced rebuild gets a verdict
ok   prebuilt: an unforced no-op is still inconclusive
ok   mtime-restoring rule: output still compared
ok   a build that produces nothing is inconclusive, not reproducible
ok   date-macro goes clean with --no-vary time
ok   file-macro goes clean with --no-vary path
ok   ls-order goes clean with --no-vary locale
ok   real build (cc): returns a verdict
     real finding: cc -> not-reproducible
       cc: build-path

105 passed, 0 failed
```

Fourteen real builds for the planted cases, four more for the prebuilt-tree,
mtime-restoring and hostile-symlink checks, plus one build of a neighbouring repo,
in about two seconds. There is no way to make that instant: a reproducibility
checker can only be tested by building things. `UNREPRO_SKIP_REAL=1` skips the
sibling build.

## The suite was non-deterministic, and the cause is the thing this repo warns about

Caught on the thirteenth consecutive run of `make test`:

```
FAIL ar-stamp goes clean with --no-vary time,umask
```

`ar rcsU` stamps the **symbol-index member** with the current time, not with
anything derived from the inputs. Verified by building two archives from one
unchanged object file a second apart: byte 34 differs, `5` against `7`,
inside the armap's timestamp. So the `ar-stamp` case was irreproducible on
the wall clock alone, and the bisection assertion that it goes clean once
`time` and `umask` are held still could only pass when both builds landed in
the same second. About one run in thirteen did not.

`t/cases/date-macro/Makefile`, in the same directory, says it first:
*"relying on the wall clock would only catch it when a second happens to
tick between the builds, which is exactly the flakiness that lets this class
of bug survive in real projects."* The same trap, in a sibling case, reached
from the other direction -- a false **positive** rather than a missed
negative, in a tool whose whole job is telling people their build is not
reproducible.

`ar rcSU` suppresses the symbol index, so the only variation left is the
member headers, which is what the case is about. The member mtimes now come
from `SOURCE_DATE_EPOCH` rather than from whenever the compiler ran, so the
`time` axis controls them. 30 consecutive clean runs.

One detail from fixing it, because it cost a second attempt: the fallback
`${SOURCE_DATE_EPOCH:-...}` must not be `1234567890`. Build A is given no
`SOURCE_DATE_EPOCH` and build B is given `EPOCH_B = 1234567890`, so a
fallback of that value made both builds agree and the **mtime** difference
disappeared, leaving only `mode` from the umask axis. The case stayed
irreproducible, so nothing failed -- it just quietly stopped demonstrating
the field it is named after.

## License

GPL-3.0-or-later.
