/* Return each directory's entries in reverse, via LD_PRELOAD.
 *
 * This is the single biggest axis unrepro could not vary. Any build
 * that globs, or walks a directory without sorting, depends on the
 * order the filesystem hands entries back. On ext4 that order is
 * stable per-directory, so the bug is invisible on the machine that
 * has it and appears on the buildd. Until this existed, an ls-based
 * file list was caught only because the LOCALE differed between the
 * two builds, which is the right answer for the wrong reason: it
 * would go on passing for a build that sorted explicitly and still
 * globbed somewhere else.
 *
 * Reversing rather than shuffling is deliberate. A random order makes
 * a failure depend on a seed nobody recorded, and the question
 * "reproducible?" must not be answered differently on two runs of the
 * tool. Reverse is the maximally different order and it is the same
 * every time.
 *
 * shortcut: not thread safe. One list of buffers, no locking, which
 * is fine for build tools walking directories one at a time and would
 * not be for a threaded one. Upgrade is a mutex around `bufs`.
 *
 * shortcut: entries are buffered per DIR* and freed in closedir. A
 * program that leaks its DIR* leaks the buffer too.
 */
#define _GNU_SOURCE
#include <dirent.h>
#include <dlfcn.h>
#include <stdlib.h>
#include <string.h>

struct buf {
	DIR *d;
	struct dirent *e;
	size_t n, i;
	int filled;
	struct buf *next;
};

static struct buf *bufs;

static struct buf *slot(DIR *d)
{
	struct buf *b;

	for (b = bufs; b; b = b->next)
		if (b->d == d)
			return b;
	b = calloc(1, sizeof *b);
	if (!b)
		return NULL;
	b->d = d;
	b->next = bufs;
	bufs = b;
	return b;
}

static void drain(struct buf *b, struct dirent *(*real)(DIR *))
{
	struct dirent *e;
	size_t cap = 0;

	while ((e = real(b->d)) != NULL) {
		if (b->n == cap) {
			size_t want = cap ? cap * 2 : 32;
			struct dirent *p = realloc(b->e, want * sizeof *p);
			if (!p)
				break;
			b->e = p;
			cap = want;
		}
		/* Copied, because the real readdir may reuse its buffer. */
		memcpy(&b->e[b->n++], e, sizeof *e);
	}
	b->filled = 1;
}

struct dirent *readdir(DIR *d)
{
	static struct dirent *(*real)(DIR *);
	struct buf *b;

	if (!real)
		real = dlsym(RTLD_NEXT, "readdir");
	if (!real)
		return NULL;

	b = slot(d);
	if (!b)
		return real(d);
	if (!b->filled)
		drain(b, real);
	if (b->i >= b->n)
		return NULL;
	/* Reversed: last entry first. */
	return &b->e[b->n - 1 - b->i++];
}

/* glibc on 64-bit exports readdir64 as a separate symbol, and that is
 * the one almost everything actually calls: the first version of this
 * shim defined only `readdir` and changed nothing at all, which looked
 * exactly like a build that was already order-independent. The two
 * share the buffer, because they walk the same DIR*. */
struct dirent64 *readdir64(DIR *d)
{
	static struct dirent64 *(*real)(DIR *);
	struct buf *b;

	if (!real)
		real = dlsym(RTLD_NEXT, "readdir64");
	if (!real)
		return NULL;

	b = slot(d);
	if (!b)
		return real(d);
	if (!b->filled) {
		struct dirent64 *e;
		size_t cap = 0;
		while ((e = real(b->d)) != NULL) {
			if (b->n == cap) {
				size_t want = cap ? cap * 2 : 32;
				struct dirent *q = realloc(b->e, want * sizeof *q);
				if (!q)
					break;
				b->e = q;
				cap = want;
			}
			memcpy(&b->e[b->n++], e, sizeof(struct dirent));
		}
		b->filled = 1;
	}
	if (b->i >= b->n)
		return NULL;
	return (struct dirent64 *)&b->e[b->n - 1 - b->i++];
}

int closedir(DIR *d)
{
	static int (*real)(DIR *);
	struct buf **p;

	if (!real)
		real = dlsym(RTLD_NEXT, "closedir");
	for (p = &bufs; *p; p = &(*p)->next)
		if ((*p)->d == d) {
			struct buf *dead = *p;
			*p = dead->next;
			free(dead->e);
			free(dead);
			break;
		}
	return real ? real(d) : -1;
}
