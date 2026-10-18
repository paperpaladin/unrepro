# Planted cause: reorder, from raw directory order rather than from
# collation.
#
# `ls -U` does not sort. Its output is whatever order readdir hands
# back, so this is invisible to the locale axis that catches
# t/cases/ls-order: change LC_ALL all you like and `ls -U` answers the
# same thing. On ext4 the order is stable per-directory, which is
# exactly why a build like this works perfectly on the machine that
# has it and produces different bytes on the buildd.
#
# The suite asserts BOTH directions on this case: caught with the
# readdir axis on, and clean with `--no-vary readdir`. The second is
# what says the readdir shim is the thing catching it, and not some
# other axis that happens to vary at the same time.
# Two listers, because glibc exports readdir and readdir64 as separate
# symbols and different programs resolve different ones. coreutils ls
# goes through readdir; CPython, built with 64-bit file offsets, goes
# through readdir64. A shim that wrapped only one would leave the
# other build order-dependent and still look like it worked, which is
# exactly what happened to the first version of the shim.
all: manifest.txt pymanifest.txt

manifest.txt:
	ls -U sub > $@

pymanifest.txt:
	python3 -c 'import os; print("\n".join(os.listdir("sub")))' > $@
