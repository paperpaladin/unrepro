# Planted cause: ar-metadata (member mtime/uid/gid/mode in the archive headers).
#
# The member ORDER is fixed here, and the member payloads are byte-identical, so
# the only difference is the 60-byte header in front of each one. That is the
# point: a tool that says "libstamp.a differs" has told you nothing, while
# "member a.o mtime A=1791267228 B=1234567890" tells you to add D.
#
# `U` is explicit because this binutils defaults to deterministic archives
# already; on distributions that do not, plain `ar rcs` behaves like this.
#
# `S` suppresses the symbol index, and it is here because of a FLAKE, caught
# on the thirteenth consecutive run of the suite:
#
#     FAIL ar-stamp goes clean with --no-vary time,umask
#
# `ar rcsU` stamps the armap -- the symbol-index member -- with the CURRENT
# TIME, not with anything derived from the inputs. Verified by building two
# archives from one unchanged object file a second apart: byte 34 differs,
# `5` against `7`, inside the armap's timestamp. So this case was
# irreproducible on the wall clock alone, and the bisection assertion that
# it goes clean once `time` and `umask` are held still could only pass when
# both builds landed in the same second. About one run in thirteen did not.
#
# That is exactly what date-macro's Makefile warns about in this same
# directory -- "relying on the wall clock would only catch it when a second
# happens to tick between the builds, which is exactly the flakiness that
# lets this class of bug survive in real projects" -- arrived at from the
# other direction, as a false POSITIVE rather than a missed negative.
#
# With `S` the only variation left is the member headers, which is what the
# case is about and what the README's worked example shows. And the member
# mtimes now come from SOURCE_DATE_EPOCH rather than from whenever the
# compiler happened to run, so the `time` axis controls them and holding it
# still makes the archive byte-identical every time instead of usually.
OBJS = a.o b.o

all: libstamp.a

libstamp.a: $(OBJS)
# The fallback must NOT be unrepro's EPOCH_B. Build A is given no
# SOURCE_DATE_EPOCH at all and build B is given EPOCH_B = 1234567890, so a
# fallback of 1234567890 made both builds agree and the mtime difference --
# the field this case exists to demonstrate, and the one in the README's
# worked example -- silently disappeared, leaving only `mode` from the umask
# axis. The case stayed irreproducible, so nothing failed; it just stopped
# showing the thing it is named after.
	touch -d "@$${SOURCE_DATE_EPOCH:-1000000000}" $(OBJS)
	ar rcSU $@ $(OBJS)
