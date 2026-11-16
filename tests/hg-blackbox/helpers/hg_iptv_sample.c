/* SPDX-License-Identifier: GPL-2.0-only */
#define _POSIX_C_SOURCE 200809L
#include <ctype.h>
#include <dirent.h>
#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#define MAX_CPUS 256
#define MAX_DEVS 8
typedef unsigned long long counter;

/* musl may report the task affinity width for _SC_NPROCESSORS_ONLN.
 * /proc/stat counters describe the whole host, so enumerate that same source.
 */
static int host_cpus(void)
{
	FILE *f = fopen("/proc/stat", "r");
	char line[2048];
	unsigned id;
	int count = 0;
	if (!f) return -1;
	while (fgets(line, sizeof(line), f)) {
		if (strncmp(line, "cpu", 3)) break;
		if (!isdigit((unsigned char)line[3])) continue;
		if (sscanf(line, "cpu%u", &id) != 1) continue;
		if (id != (unsigned)count || count == MAX_CPUS) {
			fclose(f);
			return -1;
		}
		count++;
	}
	fclose(f);
	return count;
}

static int read_number(const char *path, counter *number)
{
	FILE *f = fopen(path, "r");
	int ok = f && fscanf(f, "%llu", number) == 1;
	if (f)
		fclose(f);
	return ok;
}

static int interface_name(const char *name)
{
	if (!*name || strlen(name) > 15)
		return 0;
	for (; *name; name++)
		if (!isalnum((unsigned char)*name) && !strchr("_.:-", *name))
			return 0;
	return 1;
}

/* Include starttime in the identity so a restarted daemon is not a CPU reset. */
static int process_ticks(counter *ticks, counter *identity)
{
	DIR *directory = opendir("/proc");
	struct dirent *entry;
	if (!directory)
		return 0;
	*ticks = *identity = 0;
	while ((entry = readdir(directory))) {
		char path[128], line[4096], *q, *save = NULL;
		FILE *f;
		counter start = 0;
		int field = 3;
		if (!isdigit((unsigned char)entry->d_name[0]))
			continue;
		snprintf(path, sizeof(path), "/proc/%.32s/comm", entry->d_name);
		f = fopen(path, "r");
		if (!f)
			continue;
		q = fgets(line, sizeof(line), f);
		fclose(f);
		if (!q || strncmp(line, "rtp2httpd", 9))
			continue;
		snprintf(path, sizeof(path), "/proc/%.32s/stat", entry->d_name);
		f = fopen(path, "r");
		if (!f)
			continue;
		q = fgets(line, sizeof(line), f);
		fclose(f);
		if (!q || !(q = strrchr(line, ')')))
			continue;
		for (char *s = strtok_r(q + 2, " ", &save); s;
		     s = strtok_r(NULL, " ", &save), field++) {
			if (field == 14 || field == 15)
				*ticks += strtoull(s, NULL, 10);
			if (field == 22)
				start = strtoull(s, NULL, 10);
		}
		*identity += strtoull(entry->d_name, NULL, 10) * 1000003 + start;
	}
	closedir(directory);
	return 1;
}

int sample_main(int argc, char **argv)
{
	char *end;
	long seconds, cpus = host_cpus(), hz = sysconf(_SC_CLK_TCK);
	if (argc < 3 || argc > MAX_DEVS + 2 || cpus < 1 || cpus > MAX_CPUS || hz < 1)
		return 2;
	seconds = strtol(argv[1], &end, 10);
	if (*end || seconds < 5 || seconds > 300)
		return 2;
	for (int j = 2; j < argc; j++)
		if (!interface_name(argv[j]))
			return 2;
	setvbuf(stdout, NULL, _IOLBF, 0);
	printf("{\"meta\":true,\"hz\":%ld,\"cpus\":%ld}\n", hz, cpus);
	for (int n = 0; n <= seconds; n++) {
		char line[2048], label[32], path[192];
		counter total = 0, idle = 0, user = 0, system = 0, softirq = 0;
		counter ct[MAX_CPUS] = {0}, ci[MAX_CPUS] = {0}, ticks, identity, freq;
		counter rx[MAX_DEVS], tx[MAX_DEVS];
		int seen = 0;
		struct timespec now, delay = {1, 0};
		FILE *f = fopen("/proc/stat", "r");
		if (!f)
			return 1;
		while (fgets(line, sizeof(line), f)) {
			counter v[8] = {0}, t = 0;
			if (strncmp(line, "cpu", 3))
				break;
			if (sscanf(line, "%31s %llu %llu %llu %llu %llu %llu %llu %llu",
				   label, v, v+1, v+2, v+3, v+4, v+5, v+6, v+7) != 9) {
				fclose(f);
				return 1;
			}
			for (int i = 0; i < 8; i++) t += v[i];
			if (!strcmp(label, "cpu")) {
				total = t; idle = v[3] + v[4]; user = v[0] + v[1];
				system = v[2]; softirq = v[6];
			} else {
				int i = atoi(label + 3);
				if (i >= 0 && i < cpus) {
					ct[i] = t; ci[i] = v[3] + v[4]; seen++;
				}
			}
		}
		fclose(f);
		if (seen != cpus || !total || !process_ticks(&ticks, &identity))
			return 1;
		for (int j = 2; j < argc; j++) {
			snprintf(path, sizeof(path), "/sys/class/net/%s/statistics/rx_bytes", argv[j]);
			if (!read_number(path, &rx[j-2])) return 1;
			snprintf(path, sizeof(path), "/sys/class/net/%s/statistics/tx_bytes", argv[j]);
			if (!read_number(path, &tx[j-2])) return 1;
		}
		clock_gettime(CLOCK_MONOTONIC, &now);
		printf("{\"time\":%.6f,\"total\":%llu,\"idle\":%llu,\"user\":%llu,"
		       "\"system\":%llu,\"softirq\":%llu,\"rtp_ticks\":%llu,\"rtp_identity\":%llu,\"freq_khz\":",
		       now.tv_sec + now.tv_nsec / 1e9, total, idle, user, system, softirq, ticks, identity);
		if (read_number("/sys/devices/system/cpu/cpufreq/policy0/scaling_cur_freq", &freq))
			printf("%llu", freq);
		else printf("null");
		printf(",\"cores\":[");
		for (int i = 0; i < cpus; i++)
			printf("%s[%llu,%llu]", i ? "," : "", ct[i], ci[i]);
		printf("],\"net\":{");
		for (int j = 2; j < argc; j++)
			printf("%s\"%s\":{\"rx\":%llu,\"tx\":%llu}", j > 2 ? "," : "", argv[j], rx[j-2], tx[j-2]);
		puts("}}");
		if (n < seconds)
			nanosleep(&delay, NULL);
	}
	return 0;
}
