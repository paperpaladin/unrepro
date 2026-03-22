# Planted cause: gzip-mtime (gzip copies the input file's mtime into the 4-byte
# MTIME field of the header).
#
# gzip honours SOURCE_DATE_EPOCH for nothing at all -- that is the real lesson
# here, and `gzip -n` is the real fix. The touch below is what a build system
# effectively does for free: the input was written during the build, so it
# carries the build clock, and gzip faithfully records it.
all: data.gz

data.gz: data.txt
	touch -d "@$${SOURCE_DATE_EPOCH:-$$(date +%s)}" data.txt
	gzip -c data.txt > $@
