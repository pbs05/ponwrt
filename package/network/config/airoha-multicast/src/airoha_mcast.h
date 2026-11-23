/* SPDX-License-Identifier: GPL-2.0 WITH Linux-syscall-note */
#ifndef _UAPI_LINUX_AIROHA_MCAST_H
#define _UAPI_LINUX_AIROHA_MCAST_H
#include <linux/types.h>

#define AIROHA_MCAST_FAMILY "airoha_mcast"
#define AIROHA_MCAST_VERSION 1
enum {
	AIROHA_MCAST_CMD_UNSPEC,
	AIROHA_MCAST_CMD_GET,
	AIROHA_MCAST_CMD_APPLY,
	AIROHA_MCAST_CMD_FLUSH, /* withdraw entries; retain policy and pause state */
};
enum {
	AIROHA_MCAST_A_UNSPEC,
	AIROHA_MCAST_A_ENABLED, /* u8; consult LIVE_APPLY before runtime replacement */
	AIROHA_MCAST_A_INGRESS, /* u32 logical PON IPoE ifindex, optional VLAN */
	AIROHA_MCAST_A_EGRESS,  /* u32 bridge / bridge VLAN ifindex */
	AIROHA_MCAST_A_STATUS,  /* struct airoha_mcast_status, read-only */
	AIROHA_MCAST_A_LIVE_APPLY, /* u8 read-only; absent means boot-only */
	__AIROHA_MCAST_A_MAX,
};
#define AIROHA_MCAST_A_MAX (__AIROHA_MCAST_A_MAX - 1)

enum airoha_mcast_reject {
	AIROHA_MC_REJECT_PACKET,
	AIROHA_MC_REJECT_INGRESS,
	AIROHA_MC_REJECT_SCOPE,
	AIROHA_MC_REJECT_ROUTE,
	AIROHA_MC_REJECT_BRIDGE,
	AIROHA_MC_REJECT_RESOURCE,
	AIROHA_MC_REJECT_SAMPLE,
	AIROHA_MC_REJECT_MAX,
};

struct airoha_mcast_status {
	__u8 enabled, frozen, running, broken;
	__u8 isolated, ppe_stopped, paused, scope_lost;
	__u32 ingress, egress;
	__u16 ingress_vid, egress_vid;
	__u16 active, pending;
	__aligned_u64 epoch, learned, bound, invalidated, rejected;
	__aligned_u64 copies, delivered, keepalives, stale_copies, bad_copies, errors;
	__aligned_u64 reject[AIROHA_MC_REJECT_MAX];
};
#endif
