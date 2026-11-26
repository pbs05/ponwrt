#!/usr/bin/env python3
"""Compile the actual multicast notifier functions with host topology/MMIO stubs.

These tests exercise event selection and state transitions, not kernel locking
or hardware. Pass the prepared Linux source directory.
"""
import pathlib
import re
import subprocess
import sys
import tempfile

source = pathlib.Path(sys.argv[1])
text = (source / 'drivers/net/ethernet/airoha/airoha_mcast.c').read_text()


def function(name):
    match = re.search(r'^static (?:bool|void|int) ' + name + r'\(', text, re.M)
    if not match:
        raise RuntimeError(f'Missing function {name}')
    start = match.start()
    opening = text.index('{', start)
    depth = 1
    end = opening + 1
    while depth:
        depth += (text[end] == '{') - (text[end] == '}')
        end += 1
    return text[start:end] + '\n'


declarations = '\n'.join(re.search(r'^' + name + r' \{.*?^};', text, re.M | re.S)[0]
                         for name in ('enum mc_event_reason', 'struct mc_filter'))
prelude = r'''
#include <assert.h>
#include <arpa/inet.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
typedef uint32_t u32, __be32;
typedef uint16_t u16, vifi_t;
typedef uint8_t u8;
#define MC_MAX_FLOWS 8
#define MAXVIFS 32
#define NOTIFY_DONE 0
#define RT_TABLE_DEFAULT 253
#define RTNL_FAMILY_IPMR 128
#define ETH_P_IP 0x0800
#define container_of(p, type, member) ((type *)((char *)(p) - offsetof(type, member)))
struct notifier_block { int unused; };
struct net { int id; };
static struct net init_net;
struct net_device {
    struct net *net;
    struct net_device *upper, *real;
    int refs;
};
struct mr_mfc {
    vifi_t mfc_parent;
    union { struct { u8 ttls[MAXVIFS]; } res; } mfc_un;
};
struct mfc_cache { struct mr_mfc _c; __be32 mfc_origin, mfc_mcastgrp; };
struct mc_flow {
    struct { __be32 source, group; } sample;
    struct mr_mfc *mfc;
    u16 bridge_vid;
    vifi_t parent;
    u8 ttls[MAXVIFS];
    bool used, committed;
};
struct ipmr_offload_event { struct net *net; struct net_device *dev; __be32 group; };
struct br_ip { struct { __be32 ip4; } dst; u16 proto, vid; };
struct br_mcast_event { struct net_device *br_dev; const struct br_ip *group; };
struct netdev_notifier_info { struct net_device *dev; };
struct netdev_notifier_changeupper_info {
    struct netdev_notifier_info info;
    struct net_device *upper_dev;
};
struct switchdev_obj { int id; };
struct switchdev_notifier_port_obj_info {
    struct netdev_notifier_info info;
    const struct switchdev_obj *obj;
};
struct fib_notifier_info { int family; };
struct mfc_entry_notifier_info {
    struct fib_notifier_info info;
    struct mr_mfc *mfc;
    u32 tb_id;
};
struct vif_entry_notifier_info {
    struct fib_notifier_info info;
    struct net_device *dev;
    unsigned short vif_index, vif_flags;
    u32 tb_id;
};
struct fib_entry_notifier_info {
    struct fib_notifier_info info;
    u32 dst;
    int dst_len;
};
enum { NETDEV_CHANGENAME, NETDEV_CHANGEUPPER, NETDEV_PRECHANGEUPPER, NETDEV_UNREGISTER, NETDEV_CHANGE };
enum { SWITCHDEV_PORT_OBJ_ADD, SWITCHDEV_PORT_OBJ_DEL, SWITCHDEV_PORT_ATTR_SET };
enum { SWITCHDEV_OBJ_ID_PORT_MDB, SWITCHDEV_OBJ_ID_HOST_MDB, SWITCHDEV_OBJ_ID_PORT_VLAN };
enum { FIB_EVENT_ENTRY_REPLACE, FIB_EVENT_ENTRY_ADD, FIB_EVENT_ENTRY_DEL,
       FIB_EVENT_VIF_ADD, FIB_EVENT_VIF_DEL, FIB_EVENT_RULE_ADD,
       FIB_EVENT_RULE_DEL, FIB_EVENT_ENTRY_APPEND, FIB_EVENT_NH_DEL };
static bool net_eq(const struct net *a, const struct net *b) { return a == b; }
static struct net *dev_net(struct net_device *d) { return d->net; }
static bool is_vlan_dev(struct net_device *d) { return d->real != NULL; }
static struct net_device *vlan_dev_real_dev(struct net_device *d) { return d->real; }
static bool netdev_has_upper_dev_all_rcu(struct net_device *d, struct net_device *u) {
    while ((d = d->upper)) if (d == u) return true;
    return false;
}
static struct net_device *netdev_notifier_info_to_dev(void *p) {
    return ((struct netdev_notifier_info *)p)->dev;
}
#define switchdev_notifier_info_to_dev netdev_notifier_info_to_dev
static void dev_put(struct net_device *d) { d->refs--; }
static void rcu_read_lock(void) {}
static void rcu_read_unlock(void) {}
static void spin_lock_bh(int *p) { assert(!*p); *p = 1; }
static void spin_unlock_bh(int *p) { assert(*p); *p = 0; }
static void lockdep_assert_held(int *p) { assert(*p); }
'''
state = r'''
struct airoha_mcast {
    int lock;
    struct notifier_block mr_nb, br_nb, net_nb, fib_nb;
    struct net_device *ingress, *egress;
    struct mc_flow flows[MC_MAX_FLOWS];
    uint64_t epoch, events[MC_EVENT_MAX], unchanged, invalidated;
    bool running, scope_lost, links_dirty;
};
static void mc_remove(struct airoha_mcast *mc, struct mc_flow *f) {
    if (f->committed) mc->invalidated++;
    memset(f, 0, sizeof(*f));
}
'''
body = '\n'.join(function(name) for name in (
    'mc_matches', 'mc_route_unchanged', 'mc_invalidate_locked', 'mc_scope_dev',
    'mc_mr_notify', 'mc_br_notify', 'mc_net_notify', 'mc_sw_event', 'mc_fib_notify'))
tests = r'''
int main(void) {
    struct net_device in = {.net=&init_net}, br = {.net=&init_net};
    struct net_device out = {.net=&init_net, .real=&br};
    struct net_device port = {.net=&init_net, .upper=&br};
    struct net_device other = {.net=&init_net};
    struct airoha_mcast mc = {.ingress=&in, .egress=&out, .running=true};
    struct mfc_cache cache = {.mfc_origin=htonl(0x0a000001), .mfc_mcastgrp=htonl(0xef010101)};
    struct mfc_entry_notifier_info mfc = {{RTNL_FAMILY_IPMR}, &cache._c, RT_TABLE_DEFAULT};
    struct mc_flow flow = {.sample={cache.mfc_origin, cache.mfc_mcastgrp},
                          .bridge_vid=10, .parent=1, .mfc=&cache._c,
                          .used=true, .committed=true};
    struct mc_filter filter = {.group=cache.mfc_mcastgrp, .vid=10, .by_vid=true};
    struct ipmr_offload_event member = {&init_net, &other, cache.mfc_mcastgrp};
    struct br_ip group = {.dst={cache.mfc_mcastgrp}, .proto=htons(ETH_P_IP), .vid=10};
    struct br_mcast_event bridge = {&br, &group};
    struct netdev_notifier_changeupper_info upper = {{&port}, &br};
    struct switchdev_obj obj = {SWITCHDEV_OBJ_ID_PORT_MDB};
    struct switchdev_notifier_port_obj_info sw = {{&port}, &obj};
    struct fib_entry_notifier_info fib = {{AF_INET}, 0xc0000200, 24};
    uint64_t epoch;
    br.upper = &out;
    cache._c.mfc_parent = 1;
    memset(flow.ttls, 255, sizeof(flow.ttls));
    flow.ttls[2] = 1;
    memcpy(cache._c.mfc_un.res.ttls, flow.ttls, sizeof(flow.ttls));

    assert(mc_matches(&flow, &filter));
    filter.vid = 20;
    assert(!mc_matches(&flow, &filter));
    filter.by_vid = false;
    filter.source = htonl(0x0a000002);
    assert(!mc_matches(&flow, &filter));

    mc.flows[0] = flow;
    mc.flows[1] = flow;
    mc.flows[1].sample.group++;
    mc_fib_notify(&mc.fib_nb, FIB_EVENT_ENTRY_REPLACE, &mfc);
    assert(mc.flows[0].used && mc.flows[1].used && mc.unchanged == 1);
    assert(mc.epoch == 1 && mc.events[MC_EVENT_MFC] == 1 && !mc.invalidated);
    cache._c.mfc_un.res.ttls[3] = 1;
    mc_fib_notify(&mc.fib_nb, FIB_EVENT_ENTRY_REPLACE, &mfc);
    assert(!mc.flows[0].used && mc.flows[1].used && mc.invalidated == 1);
    mc.flows[0] = flow;
    mc.flows[0].committed = false;
    mc_fib_notify(&mc.fib_nb, FIB_EVENT_ENTRY_DEL, &mfc);
    assert(!mc.flows[0].used && mc.invalidated == 1);
    epoch = mc.epoch;
    mc_fib_notify(&mc.fib_nb, FIB_EVENT_ENTRY_DEL, &mfc);
    assert(mc.epoch == epoch + 1); /* fence not-yet-inserted snapshots */

    mc.flows[0] = flow;
    epoch = mc.epoch;
    mc_mr_notify(&mc.mr_nb, 0, &member);
    assert(mc.flows[0].used && mc.epoch == epoch);
    member.dev = &in;
    mc_mr_notify(&mc.mr_nb, 0, &member);
    assert(!mc.flows[0].used && mc.flows[1].used);

    mc.flows[0] = flow;
    mc.flows[2] = flow;
    mc.flows[2].bridge_vid = 20;
    mc_br_notify(&mc.br_nb, 0, &bridge);
    assert(!mc.flows[0].used && mc.flows[1].used && mc.flows[2].used);
    mc.flows[0] = flow;
    epoch = mc.epoch;
    group.proto = htons(0x86dd);
    mc_br_notify(&mc.br_nb, 0, &bridge);
    assert(mc.flows[0].used && mc.epoch == epoch);
    bridge.group = NULL;
    mc_br_notify(&mc.br_nb, 0, &bridge);
    assert(!mc.flows[0].used && !mc.flows[1].used && !mc.flows[2].used);

    mc.flows[0] = flow;
    assert(mc_scope_dev(&mc, &port));
    upper.info.dev = &other;
    mc_net_notify(&mc.net_nb, NETDEV_CHANGE, &upper);
    assert(mc.flows[0].used);
    upper.info.dev = &port;
    port.upper = NULL;
    mc_net_notify(&mc.net_nb, NETDEV_CHANGEUPPER, &upper);
    assert(!mc.flows[0].used); /* detached port is still related by old upper */
    port.upper = &br;

    mc.flows[0] = flow;
    epoch = mc.epoch;
    mc_sw_event(&mc, SWITCHDEV_PORT_OBJ_ADD, &sw);
    assert(mc.flows[0].used && mc.epoch == epoch); /* duplicate MDB event */
    obj.id = SWITCHDEV_OBJ_ID_PORT_VLAN;
    mc_sw_event(&mc, SWITCHDEV_PORT_OBJ_DEL, &sw);
    assert(!mc.flows[0].used);

    mc.flows[0] = flow;
    mc_fib_notify(&mc.fib_nb, FIB_EVENT_ENTRY_REPLACE, &fib);
    assert(mc.flows[0].used); /* unrelated IPv4 prefix */
    fib.dst = 0x0a000001;
    fib.dst_len = 32;
    mc_fib_notify(&mc.fib_nb, FIB_EVENT_ENTRY_DEL, &fib);
    assert(!mc.flows[0].used); /* source validation */
    mc.flows[0] = flow;
    fib.dst = 0;
    fib.dst_len = 0;
    mc_fib_notify(&mc.fib_nb, FIB_EVENT_ENTRY_REPLACE, &fib);
    assert(!mc.flows[0].used);

    mc.flows[0] = flow;
    member.dev = NULL;
    mc_mr_notify(&mc.mr_nb, 0, &member);
    assert(!mc.flows[0].used); /* policy rule change */
    mc.flows[0] = flow;
    in.refs = 1;
    upper.info.dev = &in;
    mc_net_notify(&mc.net_nb, NETDEV_UNREGISTER, &upper);
    assert(!mc.flows[0].used && mc.scope_lost && !mc.running);
    assert(!mc.ingress && in.refs == 0);
    puts("PASS: actual multicast notifier selection and revocation functions");
}
'''
with tempfile.TemporaryDirectory(prefix='hg-mcast-events-') as d:
    c = pathlib.Path(d) / 'test.c'
    binary = pathlib.Path(d) / 'test'
    c.write_text(prelude + declarations + state + body + tests)
    subprocess.run(['cc', '-std=gnu99', '-Wall', '-Wextra', '-Werror',
                    '-Wno-unused-parameter', '-fsanitize=undefined',
                    str(c), '-o', str(binary)], check=True)
    subprocess.run([str(binary)], check=True)

ipmr = (source / 'net/ipv4/ipmr.c').read_text()
for name in ('call_ipmr_mfc_entry_notifiers', 'call_ipmr_vif_entry_notifiers'):
    body = ipmr.split('static int ' + name + '(', 1)[1].split('\n}', 1)[0]
    assert 'ipmr_offload_notify' not in body
assert 'memcpy(route->ttls, c->_c.mfc_un.res.ttls' in ipmr
assert 'mc->epoch != epoch' in text
assert 'flow->bridge_vid = vid' in text
print('PASS: snapshot, generation and single MFC/VIF notification source contracts')
