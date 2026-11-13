// SPDX-License-Identifier: GPL-2.0-only
/* A boot-time controller; never processes data packets. */
#include <errno.h>
#include <linux/genetlink.h>
#include <net/if.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <unistd.h>
#include "airoha_qos.h"

struct message {
	struct nlmsghdr n;
	struct genlmsghdr g;
	unsigned char attrs[1024];
};
static unsigned int seq;

static int number(const char *s, unsigned int max, unsigned int *v)
{
	unsigned long n;
	char *end;

	if (!s[0] || strspn(s, "0123456789") != strlen(s))
		return -1;
	errno = 0;
	n = strtoul(s, &end, 10);
	if (errno || *end || n > max)
		return -1;
	*v = n;
	return 0;
}

static int profile(int argc, char **argv, uint8_t *mode, uint8_t map[64])
{
	/* rt_tos2priority((DSCP << 2) & IPTOS_TOS_MASK), ECN independent. */
	static const uint8_t legacy[8] = { 0, 0, 2, 2, 6, 6, 4, 4 };
	bool seen[64] = { false };
	int i, first = 1;

	if (!argc)
		return -1;
	if (!strcmp(argv[0], "dscp"))
		*mode = 1;
	else if (!strcmp(argv[0], "priority") || !strcmp(argv[0], "compat"))
		*mode = 0;
	else
		return -1;
	/* Pin default semantics explicitly; old configurations keep this preset. */
	if (argc > 1 && !strcmp(argv[1], "--preset")) {
		if (argc < 3 || strcmp(argv[2], "ppe-legacy-v1"))
			return -1;
		first = 3;
	}
	for (i = 0; i < 64; i++)
		map[i] = legacy[i & 7];
	for (i = first; i < argc; i++) {
		char arg[32], *colon;
		unsigned int d, q;

		if (strlen(argv[i]) >= sizeof(arg))
			return -1;
		strcpy(arg, argv[i]);
		colon = strchr(arg, ':');
		if (!colon)
			return -1;
		*colon++ = 0;
		if (number(arg, 63, &d) || number(colon, 7, &q) || seen[d])
			return -1;
		seen[d] = true;
		map[d] = q;
	}
	return 0;
}

static void init_message(struct message *m, uint16_t family, uint8_t cmd)
{
	memset(m, 0, sizeof(*m));
	m->n.nlmsg_len = NLMSG_LENGTH(GENL_HDRLEN);
	m->n.nlmsg_type = family;
	m->n.nlmsg_flags = NLM_F_REQUEST | NLM_F_ACK;
	m->n.nlmsg_seq = ++seq;
	m->g.cmd = cmd;
	m->g.version = AIROHA_QOS_VERSION;
}
static void attr(struct message *m, uint16_t type, const void *data, size_t len)
{
	struct nlattr *a = (void *)((char *)m + NLMSG_ALIGN(m->n.nlmsg_len));

	a->nla_type = type;
	a->nla_len = NLA_HDRLEN + len;
	memcpy((char *)a + NLA_HDRLEN, data, len);
	m->n.nlmsg_len = NLMSG_ALIGN(m->n.nlmsg_len) + NLA_ALIGN(a->nla_len);
}

static int parse_attrs(struct nlmsghdr *n, struct nlattr **table, unsigned int max)
{
	int len = n->nlmsg_len - NLMSG_LENGTH(GENL_HDRLEN);
	struct nlattr *a = (void *)((char *)NLMSG_DATA(n) + GENL_HDRLEN);

	memset(table, 0, (max + 1) * sizeof(*table));
	while (len) {
		unsigned int type;

		if (len < NLA_HDRLEN || a->nla_len < NLA_HDRLEN ||
		    NLA_ALIGN(a->nla_len) > len)
			return -1;
		type = a->nla_type & NLA_TYPE_MASK;
		if (type <= max) {
			if (table[type])
				return -1;
			table[type] = a;
		}
		len -= NLA_ALIGN(a->nla_len);
		a = (void *)((char *)a + NLA_ALIGN(a->nla_len));
	}
	return 0;
}
static void *value(struct nlattr *a, size_t len)
{
	return a && a->nla_len == NLA_HDRLEN + len ? (char *)a + NLA_HDRLEN : NULL;
}
static int exchange(int fd, struct message *m, unsigned char *buf, size_t size)
{
	struct sockaddr_nl kernel = { .nl_family = AF_NETLINK };
	int attempts;

	if (sendto(fd, m, m->n.nlmsg_len, 0, (void *)&kernel, sizeof(kernel)) < 0)
		return -1;
	for (attempts = 0; attempts < 16; attempts++) {
		struct sockaddr_nl from;
		socklen_t fromlen = sizeof(from);
		int len = recvfrom(fd, buf, size, 0, (void *)&from, &fromlen);
		struct nlmsghdr *n;

		if (len < 0)
			return -1;
		if (fromlen != sizeof(from) || from.nl_pid)
			continue;
		for (n = (void *)buf; NLMSG_OK(n, len); n = NLMSG_NEXT(n, len)) {
			if (n->nlmsg_seq != m->n.nlmsg_seq)
				continue;
			if (n->nlmsg_type == NLMSG_ERROR) {
				struct nlmsgerr *e;

				if (n->nlmsg_len < NLMSG_LENGTH(sizeof(*e)))
					goto bad;
				e = NLMSG_DATA(n);
				if (!e->error)
					continue;
				errno = -e->error;
				return -1;
			}
			if (n->nlmsg_type != m->n.nlmsg_type ||
			    n->nlmsg_len < NLMSG_LENGTH(GENL_HDRLEN))
				goto bad;
			memmove(buf, n, n->nlmsg_len);
			return 0;
		}
	}
bad:
	errno = EPROTO;
	return -1;
}

static void print_profile(uint8_t mode, const uint8_t *map, int frozen, int broken,
			  const uint8_t *priority_map, bool configured)
{
	int i;

	printf("{\"mode\":\"%s\",\"frozen\":%d,\"broken\":%d,\"map\":[",
	       mode ? "dscp" : priority_map ? "priority" : "compat", frozen, broken);
	for (i = 0; i < 64; i++)
		printf("%s%u", i ? "," : "", map[i]);
	printf("],\"priority_map\":");
	if (priority_map) {
		putchar('[');
		for (i = 0; i < 16; i++)
			printf("%s%u", i ? "," : "", priority_map[i]);
		putchar(']');
	} else {
		printf("null");
	}
	printf(",\"preset\":%s}\n", configured ? "\"ppe-legacy-v1\"" : "null");
}

int main(int argc, char **argv)
{
	uint8_t mode = 0, map[64];
	unsigned char buf[8192];
	struct nlattr *a[CTRL_ATTR_MAX + AIROHA_QOS_A_MAX + 1];
	struct timeval timeout = { .tv_sec = 3 };
	struct sockaddr_nl local = { .nl_family = AF_NETLINK };
	struct message m;
	uint32_t index;
	uint16_t family;
	int fd;
	bool apply;
	void *p;

	if (argc >= 3 && !strcmp(argv[1], "validate")) {
		/* Intended new policy, not a runtime kernel observation. */
		static const uint8_t priorities[16] = {
			4, 0, 0, 0, 4, 0, 7, 7, 4, 4, 4, 4, 4, 4, 4, 4
		};
		if (profile(argc - 2, argv + 2, &mode, map))
			goto usage;
		print_profile(mode, map, 0, 0, priorities, true);
		return 0;
	}
	apply = argc >= 2 && !strcmp(argv[1], "apply");
	if ((!apply && (argc != 3 || strcmp(argv[1], "get"))) ||
	    (apply && (argc < 4 || profile(argc - 3, argv + 3, &mode, map))))
		goto usage;
	index = if_nametoindex(argv[2]);
	if (!index)
		goto error;
	fd = socket(AF_NETLINK, SOCK_RAW | SOCK_CLOEXEC, NETLINK_GENERIC);
	if (fd < 0)
		goto error;
	if (setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout)) ||
	    bind(fd, (void *)&local, sizeof(local)))
		goto close_error;
	init_message(&m, GENL_ID_CTRL, CTRL_CMD_GETFAMILY);
	attr(&m, CTRL_ATTR_FAMILY_NAME, AIROHA_QOS_FAMILY, sizeof(AIROHA_QOS_FAMILY));
	if (exchange(fd, &m, buf, sizeof(buf)))
		goto close_error;
	if (parse_attrs((void *)buf, a, CTRL_ATTR_MAX) ||
	    !(p = value(a[CTRL_ATTR_FAMILY_ID], sizeof(family))))
		goto bad_reply;
	memcpy(&family, p, sizeof(family));
	init_message(&m, family, apply ? AIROHA_QOS_CMD_APPLY : AIROHA_QOS_CMD_GET);
	attr(&m, AIROHA_QOS_A_IFINDEX, &index, sizeof(index));
	if (apply) {
		attr(&m, AIROHA_QOS_A_MODE, &mode, 1);
		attr(&m, AIROHA_QOS_A_MAP, map, 64);
	}
	if (exchange(fd, &m, buf, sizeof(buf)))
		goto close_error;
	if (parse_attrs((void *)buf, a, AIROHA_QOS_A_MAX) ||
	    !value(a[AIROHA_QOS_A_MODE], 1) || !value(a[AIROHA_QOS_A_MAP], 64) ||
	    !value(a[AIROHA_QOS_A_FROZEN], 1) || !value(a[AIROHA_QOS_A_BROKEN], 1))
		goto bad_reply;
	if (a[AIROHA_QOS_A_PRIORITY_MAP] && !value(a[AIROHA_QOS_A_PRIORITY_MAP], 16))
		goto bad_reply;
	print_profile(*(uint8_t *)value(a[AIROHA_QOS_A_MODE], 1),
		      value(a[AIROHA_QOS_A_MAP], 64),
		      *(uint8_t *)value(a[AIROHA_QOS_A_FROZEN], 1),
		      *(uint8_t *)value(a[AIROHA_QOS_A_BROKEN], 1),
		      value(a[AIROHA_QOS_A_PRIORITY_MAP], 16), false);
	close(fd);
	return 0;
bad_reply:
	errno = EPROTO;
close_error:
	{ int saved = errno; close(fd); errno = saved; }
error:
	perror("airoha-qosctl");
	return 1;
usage:
	fprintf(stderr, "Usage: %s get IFNAME | apply IFNAME priority|dscp [--preset ppe-legacy-v1] [DSCP:QUEUE ...] | validate priority|dscp [--preset ppe-legacy-v1] [DSCP:QUEUE ...]\ncompat is accepted as an alias for priority. Changes take effect at boot.\n", argv[0]);
	return 2;
}
