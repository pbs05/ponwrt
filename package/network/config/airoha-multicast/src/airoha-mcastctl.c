// SPDX-License-Identifier: GPL-2.0-only
/* Policy controller; never processes data packets. */
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
#include "airoha_mcast.h"

struct message {
	struct nlmsghdr n;
	struct genlmsghdr g;
	unsigned char attrs[1024];
};
static unsigned int seq;
static bool live_apply;

static void init_message(struct message *m, uint16_t family, uint8_t cmd)
{
	memset(m, 0, sizeof(*m));
	m->n.nlmsg_len = NLMSG_LENGTH(GENL_HDRLEN);
	m->n.nlmsg_type = family;
	m->n.nlmsg_flags = NLM_F_REQUEST | NLM_F_ACK;
	m->n.nlmsg_seq = ++seq;
	m->g.cmd = cmd;
	m->g.version = AIROHA_MCAST_VERSION;
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


static void print_status(const struct airoha_mcast_status *s)
{
	static const char *const names[AIROHA_MC_REJECT_MAX] = {
		"packet", "ingress", "scope", "route", "bridge", "resource", "sample"
	};
	unsigned int i;
#define U(field) printf("\"" #field "\":%u,", (unsigned int)s->field)
#define L(field) printf("\"" #field "\":%llu,", (unsigned long long)s->field)
	printf("{\"live_apply\":%s,", live_apply ? "true" : "false");
	U(enabled); U(frozen); U(running); U(broken); U(isolated); U(ppe_stopped);
	U(paused); U(scope_lost); U(ingress); U(egress); U(ingress_vid); U(egress_vid);
	U(active); U(pending);
	L(epoch); L(learned); L(bound); L(invalidated); L(rejected); L(copies);
	L(delivered); L(keepalives); L(stale_copies); L(bad_copies); L(errors);
#undef U
#undef L
	printf("\"qualification\":\"pending\",\"reject\":{");
	for (i = 0; i < AIROHA_MC_REJECT_MAX; i++)
		printf("%s\"%s\":%llu", i ? "," : "", names[i],
		       (unsigned long long)s->reject[i]);
	puts("}}");
}

int main(int argc, char **argv)
{
	unsigned char buf[8192];
	struct nlattr *a[CTRL_ATTR_MAX + AIROHA_MCAST_A_MAX + 1];
	struct timeval timeout = { .tv_sec = 3 };
	struct sockaddr_nl local = { .nl_family = AF_NETLINK };
	struct airoha_mcast_status status;
	struct message m;
	uint32_t ingress = 0, egress = 0;
	uint16_t family;
	uint8_t enabled = 0;
	int fd;
	bool apply, flush;
	void *p;

	flush = argc == 2 && !strcmp(argv[1], "flush");
	apply = argc >= 2 && !strcmp(argv[1], "apply");
	if (apply) {
		if (argc != 3 && argc != 5)
			goto usage;
		if (!strcmp(argv[2], "0") && argc == 3)
			enabled = 0;
		else if (!strcmp(argv[2], "1") && argc == 5) {
			enabled = 1;
			ingress = if_nametoindex(argv[3]);
			egress = if_nametoindex(argv[4]);
			if (!ingress || !egress)
				goto error;
		} else
			goto usage;
	} else if (!flush && (argc != 2 || strcmp(argv[1], "get")))
		goto usage;
	fd = socket(AF_NETLINK, SOCK_RAW | SOCK_CLOEXEC, NETLINK_GENERIC);
	if (fd < 0)
		goto error;
	if (setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout)) ||
	    bind(fd, (void *)&local, sizeof(local)))
		goto close_error;
	init_message(&m, GENL_ID_CTRL, CTRL_CMD_GETFAMILY);
	attr(&m, CTRL_ATTR_FAMILY_NAME, AIROHA_MCAST_FAMILY, sizeof(AIROHA_MCAST_FAMILY));
	if (exchange(fd, &m, buf, sizeof(buf)))
		goto close_error;
	if (parse_attrs((void *)buf, a, CTRL_ATTR_MAX) ||
	    !(p = value(a[CTRL_ATTR_FAMILY_ID], sizeof(family))))
		goto bad_reply;
	memcpy(&family, p, sizeof(family));
	init_message(&m, family, apply ? AIROHA_MCAST_CMD_APPLY :
		     flush ? AIROHA_MCAST_CMD_FLUSH : AIROHA_MCAST_CMD_GET);
	if (apply) {
		attr(&m, AIROHA_MCAST_A_ENABLED, &enabled, sizeof(enabled));
		if (enabled) {
			attr(&m, AIROHA_MCAST_A_INGRESS, &ingress, sizeof(ingress));
			attr(&m, AIROHA_MCAST_A_EGRESS, &egress, sizeof(egress));
		}
	}
	if (exchange(fd, &m, buf, sizeof(buf)))
		goto close_error;
	if (parse_attrs((void *)buf, a, AIROHA_MCAST_A_MAX) ||
	    !(p = value(a[AIROHA_MCAST_A_STATUS], sizeof(status))))
		goto bad_reply;
	memcpy(&status, p, sizeof(status));
	if (a[AIROHA_MCAST_A_LIVE_APPLY]) {
		p = value(a[AIROHA_MCAST_A_LIVE_APPLY], sizeof(uint8_t));
		if (!p || *(uint8_t *)p > 1)
			goto bad_reply;
		live_apply = *(uint8_t *)p;
	}
	print_status(&status);
	close(fd);
	return 0;
bad_reply:
	errno = EPROTO;
close_error:
	{ int saved = errno; close(fd); errno = saved; }
error:
	perror("airoha-mcastctl");
	return 1;
usage:
	fprintf(stderr, "Usage: airoha-mcastctl get | flush | apply 0 | apply 1 INGRESS EGRESS\n"
		"Online replacement requires live_apply=true; hardware faults still require recovery.\n");
	return 2;
}
