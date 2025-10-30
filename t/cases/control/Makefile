# The control: the same four shapes as the broken cases, each done right. This
# case MUST report clean, and it is the more dangerous half of the oracle -- a
# checker that cries wolf on a correct build gets switched off, after which it
# catches nothing.
all: ctl sorted.txt libctl.a data.gz

# Relative source path, no debug info, no clock macros.
ctl: ctl.c
	$(CC) -O2 -o $@ $<

ctl.o: ctl.c
	$(CC) -O2 -c -o $@ $<

# Collation forced, so the ambient locale cannot reach the sort.
sorted.txt: names.in
	LC_ALL=C sort $< > $@

# D for deterministic: member mtime/uid/gid zeroed and mode normalised, so the
# umask the build ran under cannot leak into the archive.
libctl.a: ctl.o
	ar rcsD $@ ctl.o

# -n: no mtime and no original filename in the gzip header.
data.gz: data.txt
	gzip -n -c $< > $@
