/* SPDX-License-Identifier: GPL-2.0 WITH Linux-syscall-note */
#ifndef _UAPI_LINUX_AIROHA_QOS_H
#define _UAPI_LINUX_AIROHA_QOS_H

#define AIROHA_QOS_FAMILY "airoha_qos"
#define AIROHA_QOS_VERSION 1

enum {
	AIROHA_QOS_CMD_UNSPEC,
	AIROHA_QOS_CMD_GET,
	AIROHA_QOS_CMD_APPLY,
};
enum {
	AIROHA_QOS_A_UNSPEC,
	AIROHA_QOS_A_IFINDEX, /* u32: an AN7581 physical GDM interface */
	AIROHA_QOS_A_MODE,    /* u8: 0 default priority policy, 1 DSCP */
	AIROHA_QOS_A_MAP,     /* exactly 64 bytes, each 0..7 */
	AIROHA_QOS_A_FROZEN,  /* u8, read-only */
	AIROHA_QOS_A_BROKEN,  /* u8, read-only */
	AIROHA_QOS_A_PRIORITY_MAP, /* 16 bytes, read-only; absent on old kernels */
	__AIROHA_QOS_A_MAX,
};
#define AIROHA_QOS_A_MAX (__AIROHA_QOS_A_MAX - 1)
#endif
