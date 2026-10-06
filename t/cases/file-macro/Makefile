# Planted cause: build-path (__FILE__ expands to whatever path the compiler was
# handed, and $(CURDIR) makes that absolute).
#
# This is the variation that two builds in the same directory can never find.
# The leaked string also has a different LENGTH in each build, so every later
# byte in the binary shifts -- which is why the report should name the path and
# not just list a thousand differing offsets.
all: fm

fm: fm.c
	$(CC) -O2 -o $@ $(CURDIR)/fm.c
