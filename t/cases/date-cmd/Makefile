# Planted cause: timestamp (date(1) output captured into a generated header).
#
# date(1) ignores SOURCE_DATE_EPOCH, so what exposes this one is the TZ
# variation: the same instant renders differently in UTC and in Asia/Tokyo.
# Small text output on purpose, so the report has to produce a readable
# byte-offset diff and not just a verdict.
all: version.h

version.h:
	printf '#define BUILD_DATE "%s"\n' "$$(date)" > $@
