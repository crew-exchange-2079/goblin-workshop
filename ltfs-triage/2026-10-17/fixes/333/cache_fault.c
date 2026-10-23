/*
 * LD_PRELOAD fault-injection shim for issue #333 (test only, not part of LTFS).
 *  - ltfs_volume_alloc(): after the real call, set vol->index_cache_path from
 *    $LTFS_FI_CACHE (upstream never sets it itself; it is a hook for external users).
 *  - write(): fail with ENOSPC for the fd that refers to that cache file once
 *    $LTFS_FI_FAIL is set (default: fail every write to it).
 * Build: gcc -shared -fPIC -I<build>/src/libltfs -I<build> ... (see repro333.sh)
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include "ltfs.h"

int ltfs_volume_alloc(const char *execname, struct ltfs_volume **volume)
{
	int (*real)(const char *, struct ltfs_volume **) = dlsym(RTLD_NEXT, "ltfs_volume_alloc");
	int ret = real(execname, volume);
	const char *p = getenv("LTFS_FI_CACHE");
	if (ret == 0 && p && *p) {
		(*volume)->index_cache_path = strdup(p);
		fprintf(stderr, "FI: index_cache_path=%s\n", p);
	}
	return ret;
}

ssize_t write(int fd, const void *buf, size_t n)
{
	static ssize_t (*real)(int, const void *, size_t);
	const char *p = getenv("LTFS_FI_CACHE");
	if (!real)
		real = dlsym(RTLD_NEXT, "write");
	if (p && *p && !getenv("LTFS_FI_OFF") && fd > 2) {
		char link[64], tgt[4096];
		ssize_t l;
		snprintf(link, sizeof(link), "/proc/self/fd/%d", fd);
		l = readlink(link, tgt, sizeof(tgt) - 1);
		if (l > 0) {
			tgt[l] = 0;
			if (!strcmp(tgt, p)) {
				fprintf(stderr, "FI: injecting ENOSPC on cache write (%zu bytes)\n", n);
				errno = ENOSPC;
				return -1;
			}
		}
	}
	return real(fd, buf, n);
}
