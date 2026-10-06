# Planted cause: timestamp (__DATE__/__TIME__ baked into .rodata).
#
# gcc honours SOURCE_DATE_EPOCH for these macros, so varying it between the two
# builds exposes the bug deterministically. Relying on the wall clock instead
# would only catch it when a second happens to tick between the builds, which is
# exactly the flakiness that lets this class of bug survive in real projects.
#
# Compiled from a RELATIVE path and without -g on purpose, so the only thing
# that can differ is the date string -- the case has to be irreproducible in one
# way, not two.
all: dm

dm: dm.c
	$(CC) -O2 -o $@ $<
