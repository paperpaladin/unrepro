# Planted cause: ar-metadata (member mtime/uid/gid/mode in the archive headers).
#
# The member ORDER is fixed here, and the member payloads are byte-identical, so
# the only difference is the 60-byte header in front of each one. That is the
# point: a tool that says "libstamp.a differs" has told you nothing, while
# "member a.o mtime A=1791267228 B=1234567890" tells you to add D.
#
# `U` is explicit because this binutils defaults to deterministic archives
# already; on distributions that do not, plain `ar rcs` behaves like this.
OBJS = a.o b.o

all: libstamp.a

libstamp.a: $(OBJS)
	ar rcsU $@ $(OBJS)
