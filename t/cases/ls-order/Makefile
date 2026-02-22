# Planted cause: reorder (file list built from ls, whose sort is collation- and
# therefore locale-dependent).
#
# Under LC_ALL=C this is a byte sort: Beta, Gamma, _under, alpha. Under any
# en_*.UTF-8 locale glibc folds case and skips punctuation: alpha, Beta, Gamma,
# _under. Same files, same bytes, different order -- which is why the
# diagnosis has to be "permutation", not "differs".
all: manifest.txt

manifest.txt:
	ls *.in > $@
