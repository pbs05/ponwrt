/* SPDX-License-Identifier: GPL-2.0-only */
/* Read-only socket diagnostics and kernel counters; no subscription or traffic. */
#define _GNU_SOURCE
#include <arpa/inet.h>
#include <dirent.h>
#include <errno.h>
#include <linux/inet_diag.h>
#include <linux/netlink.h>
#include <linux/rtnetlink.h>
#include <linux/sock_diag.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <unistd.h>

static void quoted(const char *s)
{
	putchar('"');
	for (const unsigned char *p = (const void *)s; *p; p++) {
		if (*p == '"' || *p == '\\') printf("\\%c", *p);
		else if (*p < 32) printf("\\u%04x", *p);
		else putchar(*p);
	}
	putchar('"');
}

static void file_value(const char *name, const char *path)
{
	char data[65536];
	FILE *f = fopen(path, "r");
	quoted(name); putchar(':');
	if (!f) { printf("null"); return; }
	size_t n = fread(data, 1, sizeof(data)-1, f);
	if (ferror(f) || !feof(f)) { fclose(f); printf("null"); return; }
	fclose(f); data[n] = 0; quoted(data);
}

static void owners(void)
{
	DIR *d = opendir("/proc"); struct dirent *e; int comma = 0;
	putchar('[');
	while (d && (e = readdir(d))) {
		char path[256], comm[64];
		if (e->d_name[0] < '0' || e->d_name[0] > '9') continue;
		snprintf(path, sizeof(path), "/proc/%.32s/comm", e->d_name);
		FILE *f = fopen(path, "r");
		if (!f) continue;
		char *ok = fgets(comm, sizeof(comm), f); fclose(f);
		/* Architecture-suffixed fnOS binaries have a truncated task comm. */
		if (!ok || strncmp(comm, "rtp2httpd", 9) ||
		    (comm[9] != '\n' && comm[9] != '-')) continue;
		snprintf(path, sizeof(path), "/proc/%.32s/fd", e->d_name);
		DIR *fd = opendir(path); struct dirent *entry;
		while (fd && (entry = readdir(fd))) {
			char link[128], target[256]; unsigned long inode;
			snprintf(target, sizeof(target), "/proc/%.32s/fd/%.32s", e->d_name, entry->d_name);
			ssize_t n = readlink(target, link, sizeof(link)-1);
			if (n < 0) continue;
			link[n] = 0;
			if (sscanf(link, "socket:[%lu]", &inode) != 1) continue;
			printf("%s{\"pid\":%u,\"inode\":%lu}", comma ? "," : "", (unsigned)strtoul(e->d_name,NULL,10), inode);
			comma = 1;
		}
		if (fd) closedir(fd);
	}
	if (d) closedir(d);
	putchar(']');
}

static int sockets(void)
{
	int fd = socket(AF_NETLINK, SOCK_DGRAM | SOCK_CLOEXEC, NETLINK_SOCK_DIAG);
	struct timeval timeout = { .tv_sec = 2 };
	struct {
		struct nlmsghdr h;
		struct inet_diag_req_v2 r;
	} request = {
		.h = { .nlmsg_len = NLMSG_LENGTH(sizeof(struct inet_diag_req_v2)),
			.nlmsg_type = SOCK_DIAG_BY_FAMILY, .nlmsg_flags = NLM_F_REQUEST | NLM_F_DUMP, .nlmsg_seq = 1 },
		.r = { .sdiag_family = AF_INET, .sdiag_protocol = IPPROTO_UDP, .idiag_states = ~0U,
			.idiag_ext = 1 << (INET_DIAG_SKMEMINFO-1) },
	};
	struct sockaddr_nl kernel = { .nl_family = AF_NETLINK };
	int complete = 0, comma = 0;
	putchar('[');
	if (fd < 0) goto done;
	setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout));
	if (sendto(fd,&request,sizeof(request),0,(void*)&kernel,sizeof(kernel)) < 0) goto done;
	for (int batch = 0; batch < 128; batch++) {
		_Alignas(struct nlmsghdr) char buffer[32768];
		int n = recv(fd, buffer, sizeof(buffer), 0);
		if (n <= 0) break;
		for (struct nlmsghdr *h = (void*)buffer; NLMSG_OK(h,n); h = NLMSG_NEXT(h,n)) {
			if (h->nlmsg_seq != 1) continue;
			if (h->nlmsg_type == NLMSG_DONE) { complete = !(h->nlmsg_flags & NLM_F_DUMP_INTR); goto done; }
			if (h->nlmsg_type == NLMSG_ERROR) goto done;
			if (h->nlmsg_type != SOCK_DIAG_BY_FAMILY || h->nlmsg_len < NLMSG_LENGTH(sizeof(struct inet_diag_msg))) continue;
			struct inet_diag_msg *m = NLMSG_DATA(h);
			int len = h->nlmsg_len - NLMSG_LENGTH(sizeof(*m));
			struct rtattr *a = (void*)(m+1);
			char address[INET_ADDRSTRLEN];
			inet_ntop(AF_INET, m->id.idiag_src, address, sizeof(address));
			printf("%s{\"inode\":%u,\"address\":\"%s\",\"port\":%u,\"queue_bytes\":%u",
				comma ? "," : "", m->idiag_inode, address, ntohs(m->id.idiag_sport), m->idiag_rqueue);
			for (; RTA_OK(a,len); a = RTA_NEXT(a,len)) {
				if (a->rta_type != INET_DIAG_SKMEMINFO || RTA_PAYLOAD(a) < (SK_MEMINFO_DROPS+1)*sizeof(unsigned)) continue;
				unsigned *mem = RTA_DATA(a);
				printf(",\"rcvbuf_bytes\":%u,\"allocated_bytes\":%u,\"drops\":%u",
					mem[SK_MEMINFO_RCVBUF], mem[SK_MEMINFO_RMEM_ALLOC], mem[SK_MEMINFO_DROPS]);
			}
			putchar('}'); comma = 1;
		}
	}
done:
	if (fd >= 0) close(fd);
	putchar(']');
	return complete;
}

int diag_main(int argc, char **argv)
{
	(void)argv;
	if (argc != 1) return 2;
	putchar('{'); file_value("boot_id", "/proc/sys/kernel/random/boot_id");
	printf(","); file_value("uptime", "/proc/uptime");
	printf(","); file_value("snmp", "/proc/net/snmp");
	printf(","); file_value("softnet", "/proc/net/softnet_stat");
	printf(","); file_value("interrupts", "/proc/interrupts");
	printf(","); file_value("softirqs", "/proc/softirqs");
	printf(","); file_value("meminfo", "/proc/meminfo");
	printf(","); file_value("udp", "/proc/net/udp");
	printf(","); file_value("udp6", "/proc/net/udp6");
	printf(",\"rtp2httpd_fds\":"); owners();
	printf(",\"udp_sockets\":"); int complete = sockets();
	printf(",\"socket_diag_complete\":%s}\n", complete ? "true" : "false");
	return 0;
}
