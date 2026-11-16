/* SPDX-License-Identifier: GPL-2.0-only */
/* Bounded, read-only AN7581 PSE observer. Never write device registers. */
#define _POSIX_C_SOURCE 200809L
#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <time.h>
#include <unistd.h>

static int target_board(void)
{
	char board[128] = {0};
	int fd = open("/tmp/sysinfo/board_name", O_RDONLY);
	if (fd < 0) return 0;
	ssize_t n = read(fd, board, sizeof(board)-1);
	close(fd);
	return n > 0 && !strncmp(board, "nokia,xg-040g-md", 16);
}

static int fault_snapshot(const char *directory, unsigned fq, unsigned isr)
{
	char path[256], buffer[4096];
	if (!directory) return 0;
	/* The controller creates a private, unique directory before this probe. */
	if (strncmp(directory, "/root/hiptv_pse_", 16) || strlen(directory) > 200 ||
	    strchr(directory + 16, '/') || strstr(directory, "..")) return -1;
	snprintf(path, sizeof(path), "%s/fault.txt", directory);
	int out = open(path, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW, 0600);
	if (out < 0) return -1;
	int n = snprintf(buffer, sizeof(buffer), "fq=%u isr=%08x\n", fq, isr);
	if (write(out, buffer, (size_t)n) != n || fsync(out)) { close(out); return -1; }
	int in = open("/sys/kernel/debug/ppe/multicast_transactions", O_RDONLY);
	if (in >= 0) {
		ssize_t count;
		/* Optional diagnostic-image journal; normal images omit this file.
		 * At most 64 KiB, never a full PPE dump.
		 */
		for (unsigned total = 0; total < 65536 &&
		     (count = read(in, buffer, sizeof(buffer))) > 0; total += (unsigned)count)
			if (write(out, buffer, (size_t)count) != count) break;
		close(in);
	}
	int rc = fsync(out);
	close(out);
	return rc;
}

int pse_main(int argc, char **argv)
{
	char *end;
	if (argc < 2 || argc > 3) return 2;
	long seconds = strtol(argv[1], &end, 10);
	if (*end || seconds < 1 || seconds > 280 || !target_board()) return 2;
	int fd = open("/dev/mem", O_RDONLY | O_SYNC);
	if (fd < 0) return 1;
	volatile uint32_t *fe = mmap(NULL, 4096, PROT_READ, MAP_SHARED, fd, 0x1fb50000);
	close(fd);
	if (fe == MAP_FAILED) return 1;
	setvbuf(stdout, NULL, _IOLBF, 0);
	struct timespec start, now, delay = { .tv_nsec = 50000000 };
	clock_gettime(CLOCK_MONOTONIC, &start);
	int failed = 0;
	for (;;) {
		clock_gettime(CLOCK_MONOTONIC, &now);
		double elapsed = now.tv_sec-start.tv_sec + (now.tv_nsec-start.tv_nsec)/1e9;
		uint32_t raw = fe[0x108/4], isr = fe[0x08/4];
		unsigned fq = raw & 0x7fff;
		printf("{\"elapsed\":%.6f,\"monotonic\":%.6f,\"fq\":%u,\"raw_fq\":%u,\"isr\":%u,\"selector\":%u,\"record\":%u,\"shared\":%u}\n",
		       elapsed, now.tv_sec + now.tv_nsec/1e9, fq, raw, isr,
		       fe[0x80/4], fe[0x100/4], fe[0x104/4]);
		if (fq < 1024 || (isr & (1U << 8))) {
			/* Persist the first fault before any optional journal read. */
			fault_snapshot(argc == 3 ? argv[2] : NULL, fq, isr);
			failed = 1;
			break;
		}
		if (elapsed >= seconds) break;
		nanosleep(&delay, NULL);
	}
	munmap((void *)fe, 4096);
	return failed;
}

int health_main(int argc, char **argv)
{
	struct in_addr address;
	char *end, command[128], line[512];
	if (argc != 3 || inet_pton(AF_INET, argv[1], &address) != 1) return 2;
	long seconds = strtol(argv[2], &end, 10);
	if (*end || seconds < 1 || seconds > 280) return 2;
	snprintf(command, sizeof(command), "ping -n -c 1 -W 1 %s 2>/dev/null", argv[1]);
	setvbuf(stdout, NULL, _IOLBF, 0);
	struct timespec start, now, delay = { .tv_sec = 1 };
	clock_gettime(CLOCK_MONOTONIC, &start);
	for (;;) {
		clock_gettime(CLOCK_MONOTONIC, &now);
		double elapsed = now.tv_sec-start.tv_sec + (now.tv_nsec-start.tv_nsec)/1e9;
		if (elapsed >= seconds) return 0;
		double rtt = -1;
		FILE *f = popen(command, "r");
		while (f && fgets(line, sizeof(line), f)) {
			char *q = strstr(line, "time=");
			if (q) rtt = strtod(q+5, NULL);
		}
		int ok = f && pclose(f) == 0 && rtt >= 0;
		printf("{\"elapsed\":%.6f,\"ok\":%s,\"rtt_ms\":%.6f}\n", elapsed, ok?"true":"false", rtt);
		if (!ok) return 1;
		nanosleep(&delay, NULL);
	}
}
