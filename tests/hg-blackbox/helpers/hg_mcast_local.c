// SPDX-License-Identifier: GPL-2.0-only
/* Temporary HG application receiver; no AF_PACKET/promiscuous capture.
 * Build with the firmware toolchain, copy explicitly to /tmp, then invoke:
 * hg-mcast-local IFACE GROUP PORT SOURCE SPORT TOKEN
 * stdin: join, leave, clear, collect PHASE, quit. JSON replies on stdout.
 * Membership is socket-scoped and an independent 15-minute deadline applies.
 */
#define _GNU_SOURCE
#include <arpa/inet.h>
#include <errno.h>
#include <net/if.h>
#include <poll.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>

#define PHASES 32
#define SEQUENCES 10000
struct record {
	unsigned packets, duplicates, invalid;
	unsigned tos[256], ttl[256], length[9001];
	bool seen[SEQUENCES];
};
static struct record records[PHASES];
static unsigned drops, limited;
static unsigned char token[16];
static struct in_addr source, group;
static unsigned index_no, port, sport;
static const char *interface;

static long now(void)
{
	struct timespec t;
	clock_gettime(CLOCK_MONOTONIC, &t);
	return t.tv_sec;
}

static int number(const char *s, unsigned low, unsigned high)
{
	char *end;
	unsigned long v;
	if (!*s || strspn(s, "0123456789") != strlen(s))
		return -1;
	errno = 0;
	v = strtoul(s, &end, 10);
	return errno || *end || v < low || v > high ? -1 : (int)v;
}

static int join(void)
{
	struct sockaddr_in addr = { .sin_family = AF_INET, .sin_port = htons(port), .sin_addr = group };
	struct ip_mreqn req = { .imr_multiaddr = group, .imr_ifindex = index_no };
	int fd = socket(AF_INET, SOCK_DGRAM | SOCK_CLOEXEC | SOCK_NONBLOCK, 0);
	int one = 1, zero = 0, buffer = 4 * 1024 * 1024;
	if (fd < 0)
		return -1;
	if (setsockopt(fd, SOL_SOCKET, SO_BINDTODEVICE, interface, strlen(interface) + 1) ||
	    setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one)) ||
	    setsockopt(fd, SOL_SOCKET, SO_RCVBUF, &buffer, sizeof(buffer)) ||
	    setsockopt(fd, SOL_SOCKET, SO_RXQ_OVFL, &one, sizeof(one)) ||
	    setsockopt(fd, IPPROTO_IP, IP_MULTICAST_ALL, &zero, sizeof(zero)) ||
	    setsockopt(fd, IPPROTO_IP, IP_RECVTOS, &one, sizeof(one)) ||
	    setsockopt(fd, IPPROTO_IP, IP_RECVTTL, &one, sizeof(one)) ||
	    setsockopt(fd, IPPROTO_IP, IP_PKTINFO, &one, sizeof(one)) ||
	    bind(fd, (void *)&addr, sizeof(addr)) ||
	    setsockopt(fd, IPPROTO_IP, IP_ADD_MEMBERSHIP, &req, sizeof(req))) {
		close(fd);
		return -1;
	}
	return fd;
}

static void datagram(int fd)
{
	unsigned char body[9000], control[256];
	struct sockaddr_in from;
	struct iovec iov = { .iov_base = body, .iov_len = sizeof(body) };
	struct msghdr msg = { .msg_name = &from, .msg_namelen = sizeof(from),
		.msg_iov = &iov, .msg_iovlen = 1, .msg_control = control, .msg_controllen = sizeof(control) };
	struct cmsghdr *cm;
	struct record *r;
	int len = recvmsg(fd, &msg, 0), ttl = -1, tos = -1, ifindex = 0;
	uint16_t phase;
	uint32_t seq;
	if (len < 30 || memcmp(body, "HGMCAST1", 8) || memcmp(body + 8, token, 16))
		return;
	if (msg.msg_flags & (MSG_TRUNC | MSG_CTRUNC)) {
		limited++;
		return;
	}
	memcpy(&phase, body + 24, 2);
	memcpy(&seq, body + 26, 4);
	phase = ntohs(phase);
	seq = ntohl(seq);
	if (phase >= PHASES || seq >= SEQUENCES || len + 28 > 9000) {
		limited++;
		return;
	}
	for (cm = CMSG_FIRSTHDR(&msg); cm; cm = CMSG_NXTHDR(&msg, cm)) {
		if (cm->cmsg_level == IPPROTO_IP) {
			if (cm->cmsg_type == IP_TOS && cm->cmsg_len >= CMSG_LEN(1))
				tos = *(unsigned char *)CMSG_DATA(cm);
			if (cm->cmsg_type == IP_TTL && cm->cmsg_len >= CMSG_LEN(sizeof(int)))
				memcpy(&ttl, CMSG_DATA(cm), sizeof(ttl));
			if (cm->cmsg_type == IP_PKTINFO && cm->cmsg_len >= CMSG_LEN(sizeof(struct in_pktinfo))) {
				struct in_pktinfo info;
				memcpy(&info, CMSG_DATA(cm), sizeof(info));
				ifindex = info.ipi_addr.s_addr == group.s_addr ? info.ipi_ifindex : 0;
			}
		} else if (cm->cmsg_level == SOL_SOCKET && cm->cmsg_type == SO_RXQ_OVFL &&
			   cm->cmsg_len >= CMSG_LEN(sizeof(drops))) {
			memcpy(&drops, CMSG_DATA(cm), sizeof(drops));
		}
	}
	r = &records[phase];
	r->packets++;
	r->duplicates += r->seen[seq];
	r->seen[seq] = true;
	if (ttl < 0 || ttl > 255 || tos < 0 || ifindex != (int)index_no ||
	    from.sin_addr.s_addr != source.s_addr || ntohs(from.sin_port) != sport)
		r->invalid++;
	else {
		r->ttl[ttl]++;
		r->tos[tos]++;
	}
	r->length[len + 28]++;
}

static void histogram(const unsigned *values, unsigned n, unsigned shift, unsigned mask)
{
	unsigned sum[9001] = {}, i, comma = 0;
	for (i = 0; i < n; i++)
		sum[(i >> shift) & mask] += values[i];
	putchar('{');
	for (i = 0; i < n && i <= mask; i++)
		if (sum[i]) {
			printf("%s\"%u\":%u", comma ? "," : "", i, sum[i]);
			comma = 1;
		}
	putchar('}');
}

static void collect(unsigned phase)
{
	struct record *r = &records[phase];
	unsigned i, comma = 0;
	printf("{\"packets\":%u,\"duplicates\":%u,\"invalid_metadata\":%u,\"udp_drops\":%u,\"limited\":%u,\"sequences\":[",
	       r->packets, r->duplicates, r->invalid, drops, limited);
	for (i = 0; i < SEQUENCES; i++)
		if (r->seen[i]) {
			printf("%s%u", comma ? "," : "", i);
			comma = 1;
		}
	printf("],\"ttl\":"); histogram(r->ttl, 256, 0, 255);
	printf(",\"dscp\":"); histogram(r->tos, 256, 2, 63);
	printf(",\"ecn\":"); histogram(r->tos, 256, 0, 3);
	printf(",\"lengths\":"); histogram(r->length, 9001, 0, 16383);
	puts("}");
}

int main(int argc, char **argv)
{
	char line[80];
	long deadline = now() + 900;
	int fd = -1, i;
	if (argc != 7 || strlen(argv[6]) != 32 || strspn(argv[6], "0123456789abcdefABCDEF") != 32)
		return 2;
	interface = argv[1];
	index_no = if_nametoindex(interface);
	i = number(argv[3], 1024, 65535);
	if (!index_no || i < 0)
		return 2;
	port = i;
	i = number(argv[5], 1024, 65535);
	if (i < 0 || inet_pton(AF_INET, argv[2], &group) != 1 ||
	    inet_pton(AF_INET, argv[4], &source) != 1 || (ntohl(group.s_addr) >> 24) != 239)
		return 2;
	sport = i;
	for (i = 0; i < 16; i++) {
		char pair[3] = { argv[6][i * 2], argv[6][i * 2 + 1], 0 };
		token[i] = strtoul(pair, NULL, 16);
	}
	setvbuf(stdout, NULL, _IOLBF, 0);
	printf("{\"ready\":true,\"ifindex\":%u}\n", index_no);
	while (now() < deadline) {
		struct pollfd pollfds[2] = { { .fd = 0, .events = POLLIN }, { .fd = fd, .events = POLLIN } };
		if (poll(pollfds, 2, 1000) < 0) {
			if (errno == EINTR)
				continue;
			break;
		}
		if (fd >= 0 && (pollfds[1].revents & POLLIN))
			datagram(fd);
		if (pollfds[0].revents & (POLLIN | POLLHUP | POLLERR)) {
			if (!fgets(line, sizeof(line), stdin) || !strcmp(line, "quit\n"))
				break;
			line[strcspn(line, "\n")] = 0;
			if (!strcmp(line, "join")) {
				if (fd < 0)
					fd = join();
				puts(fd < 0 ? "{\"error\":\"join_failed\"}" : "{\"ok\":true}");
			} else if (!strcmp(line, "leave")) {
				if (fd >= 0) close(fd);
				fd = -1;
				puts("{\"ok\":true}");
			} else if (!strcmp(line, "clear")) {
				memset(records, 0, sizeof(records));
				limited = 0;
				puts("{\"ok\":true}");
			} else if (!strncmp(line, "collect ", 8) && (i = number(line + 8, 0, PHASES - 1)) >= 0) {
				collect(i);
			} else {
				puts("{\"error\":\"invalid_command\"}");
			}
		}
	}
	if (fd >= 0) close(fd);
	return 0;
}
