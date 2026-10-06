# The suite runs fourteen real builds (seven cases, twice each) plus one build of
# a neighbouring repo, so it is seconds rather than milliseconds. There is no way
# around that: a reproducibility checker can only be tested by building things.
test:
	@python3 t/test_unrepro.py

# Nothing here is generated -- check builds in a fresh temp directory and removes
# it -- so this only sweeps the bytecode cache and anything --keep left behind.
clean:
	rm -rf __pycache__ t/__pycache__
	rm -rf /tmp/unrepro.*

.PHONY: test clean
