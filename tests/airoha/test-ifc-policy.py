#!/usr/bin/env python3
"""Compile actual IFC policy/snapshot/work functions against bounded host mocks.

Does not claim to emulate the ASIC. Register semantics have separate live
evidence; these checks exercise VLAN lifecycle and fail-open policy assembly.
Usage: test-ifc-policy.py PREPARED_KERNEL
"""
import pathlib
import re
import subprocess
import sys
import tempfile

root = pathlib.Path(sys.argv[1])
source = (root / 'drivers/net/ethernet/airoha/airoha_ifc.c').read_text()


def function(name):
    m = re.search(r'^static (?:int |bool |void |const char \*)' + name + r'\(', source, re.M)
    start = m.start()
    end = source.index('{', start) + 1
    depth = 1
    while depth:
        depth += (source[end] == '{') - (source[end] == '}')
        end += 1
    return source[start:end] + '\n'


prelude = r'''
#include <arpa/inet.h>
#include <assert.h>
#include <errno.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdarg.h>
#include <stdio.h>
#include <string.h>
typedef uint32_t u32, __be32;
typedef uint64_t u64;
typedef uint16_t u16;
#define BIT(n) (1U << (n))
#define GENMASK(h,l) ((UINT32_MAX >> (31-(h))) & (UINT32_MAX << (l)))
#define GENMASK_ULL(h,l) ((UINT64_MAX >> (63-(h))) & (UINT64_MAX << (l)))
#define current get_current()
#define ARRAY_SIZE(a) (sizeof(a)/sizeof((a)[0]))
#define U32_MAX UINT32_MAX
#define ETH_P_IP 0x800
#define ETH_P_8021Q 0x8100
#define NETREG_REGISTERED 1
#define container_of(p,t,m) ((t *)((char *)(p)-offsetof(t,m)))
typedef int spinlock_t;
struct mutex { int locked; };
struct work_struct { int queued; };
struct notifier_block { int unused; };
struct seq_file { void *private; char output[32768]; unsigned length; };
static void seq_printf(struct seq_file *s, const char *fmt, ...) {
 va_list args; va_start(args,fmt);
 s->length+=vsnprintf(s->output+s->length,sizeof(s->output)-s->length,fmt,args);
 va_end(args);
}
static void seq_putc(struct seq_file *s, char c) { seq_printf(s,"%c",c); }
struct list_head { int unused; };
struct ip_mc_list { __be32 multiaddr; struct ip_mc_list *next_rcu; };
struct in_device { struct ip_mc_list *mc_list; };
struct net_device {
 struct net_device *next, *real, *upper;
 struct in_device idev;
 int net, reg_state, running, bridge, route_error;
 u16 vid, proto;
 __be32 routed;
};
struct airoha_eth { int dev; u32 cfg, port; };
static int init_net, rtnl, program_error, race;
static struct net_device *devices;
static int system_unbound_wq;
#define ASSERT_RTNL() assert(rtnl)
#define rtnl_dereference(p) (ASSERT_RTNL(), (p))
#define dev_net(d) (&(d)->net)
#define net_eq(a,b) (*(a)==*(b))
#define is_vlan_dev(d) ((d)->real != NULL)
#define vlan_dev_real_dev(d) ((d)->real)
#define vlan_dev_vlan_proto(d) ((d)->proto)
#define vlan_dev_vlan_id(d) ((d)->vid)
#define netif_running(d) ((d)->running)
#define netif_is_bridge_port(d) ((d)->bridge)
#define __in_dev_get_rtnl(d) (&(d)->idev)
#define for_each_netdev(n,d) for((d)=devices; (d); (d)=(d)->next)
#define netdev_for_each_upper_dev_rcu(d,u,it) \
 for((it)=NULL,(u)=(d)->upper; (u); (u)=NULL)
#define rcu_read_lock() ((void)0)
#define rcu_read_unlock() ((void)0)
#define ipv4_is_multicast(g) ((ntohl(g)>>28)==14)
#define ipv4_is_local_multicast(g) ((ntohl(g)>>8)==0xe00000)
#define spin_lock_irqsave(l,f) do { (f)=0; assert(!*(l)); *(l)=1; } while(0)
#define spin_unlock_irqrestore(l,f) do { (void)(f); assert(*(l)); *(l)=0; } while(0)
#define lockdep_assert_held(l) assert(*(l))
#define dev_warn(...) ((void)0)
static void mutex_lock(struct mutex *m) { assert(!m->locked); m->locked=1; }
static void mutex_unlock(struct mutex *m) { assert(m->locked); m->locked=0; }
static void rtnl_lock(void) { assert(!rtnl); rtnl=1; }
static void rtnl_unlock(void) { assert(rtnl); rtnl=0; }
static void queue_work(int q, struct work_struct *w) { (void)q; w->queued=1; }
static int ipmr_offload_group_snapshot(struct net_device *d, __be32 *g,
                                       unsigned *n, unsigned cap) {
 if(d->route_error) return d->route_error;
 if(!d->routed) return 0;
 for(unsigned i=0;i<*n;i++) if(g[i]==d->routed) return 0;
 if(*n==cap) return -ENOSPC;
 g[(*n)++]=d->routed; return 0;
}
'''
types = source[source.index('#define IFC_PORT'):source.index('static int ifc_request')]
registers = r'''
static void airoha_fe_wr(struct airoha_eth *e, unsigned off, u32 v) {
 if(off==IFC_CFG) e->cfg=v; else { assert(off==IFC_PORT); e->port=v; }
}
static u32 airoha_fe_rr(struct airoha_eth *e, unsigned off) {
 assert(off==IFC_CFG || off==IFC_PORT); return off==IFC_CFG ? e->cfg : e->port;
}
'''
program = r'''
static int ifc_program(struct airoha_ifc *f) {
 assert(!f->active && !(f->eth->cfg & IFC_ENABLE));
 if(race) ifc_changed(f);
 return program_error;
}
static int ifc_mib(struct airoha_ifc *f, u64 *packets) {
 assert(f->mutex.locked); *packets=123; return 0;
}
'''
tests = r'''
struct packet { u32 group; unsigned vid, proto; bool tagged, ppp, qinq, ipv6; };
static bool dropped(struct airoha_ifc *f, struct packet p) {
 if(!f->active) return false;
 u32 key[9]={p.ipv6?0:2,(p.tagged?BIT(5):0)|(p.ppp?BIT(10):0)|(p.qinq?BIT(4):0),
             p.vid<<20,p.ipv6?0x86dd:0x800,0,0,0,0,IFC_PON};
 for(unsigned i=0;i<f->lut_count;i++) {
  u32 k=f->lut[i][0], format=f->lut[i][1];
  if(!(format & BIT(15))) continue;
  unsigned field=format & 63;
  bool hit=false;
  if(format & BIT(13)) {
   assert(field==3);
   unsigned prefix=(format>>8)&31;
   u32 mask=prefix ? UINT32_MAX<<(32-prefix) : UINT32_MAX;
   hit=(p.group & mask)==(k & mask);
  } else {
   assert(field==24 || field==18);
   unsigned value=field==24 ? p.proto : p.vid;
   hit=(value & (k>>16))==(k & 0xffff);
  }
  if(hit) key[4]|=BIT(i);
 }
 for(unsigned i=0;i<f->rule_count;i++) {
  struct ifc_rule *r=&f->rules[i]; bool match=true;
  /* Allowed packets must MISS every rule, not hit a terminal no-op. */
  assert(r->action[2]==BIT(28) && r->action[5]==(15U<<8));
  for(unsigned j=0;j<9;j++) if((key[j]^r->key[j]) & r->mask[j]) match=false;
  if(match) return !!(r->action[2]&BIT(28));
 }
 return false;
}
static void update(struct airoha_ifc *f) {
 ifc_changed(f); assert(!f->active && !(f->eth->cfg&1));
 ifc_work(&f->work);
}
int main(void) {
 struct airoha_eth eth={.cfg=0x00ffbb1e,.port=0xfffffe0e};
 struct net_device pon={.running=1,.reg_state=NETREG_REGISTERED};
 struct net_device vlan={.real=&pon,.vid=4000,.proto=htons(ETH_P_8021Q),.running=1};
 struct net_device second={.real=&pon,.vid=4001,.proto=htons(ETH_P_8021Q),.running=1};
 struct net_device lan={.running=1};
 struct ip_mc_list member={.multiaddr=htonl(0xef03017c)};
 struct airoha_ifc f={.eth=&eth,.pon=&pon,.ready=true,.owned=true,
                     .saved_cfg=eth.cfg,.saved_port=eth.port};
 struct packet p={.group=0xef03017c,.vid=4000,.proto=17,.tagged=true};
 devices=&pon; pon.next=&vlan; vlan.next=&second; second.next=&lan;
 update(&f); assert(f.active && dropped(&f,p));
 assert(!strcmp(ifc_state(&f), "active"));
 assert(f.applied.generation==f.generation && f.applied.groups==0);
 vlan.idev.mc_list=&member;
 update(&f); assert(!dropped(&f,p));
 assert(f.applied.count==1 && f.applied.scopes[0].vid==4000);
 assert(f.applied.groups==1 && f.applied.scopes[0].groups);
 p.vid=4001; assert(dropped(&f,p)); /* Same G is not a global exception. */
 second.running=0;
 vlan.vid=4001; update(&f); /* Recreate/retag the IPTV interface. */
 assert(f.applied.scopes[0].vid==4001);
 assert(!dropped(&f,p)); p.vid=4000; assert(dropped(&f,p));
 vlan.running=0; update(&f); p.vid=4001; assert(dropped(&f,p));
 vlan.running=1; update(&f); assert(!dropped(&f,p));
 vlan.idev.mc_list=NULL; lan.idev.mc_list=&member;
 update(&f); assert(dropped(&f,p)); /* Unrelated local LAN join. */
 vlan.routed=member.multiaddr; update(&f); assert(!dropped(&f,p));
 vlan.routed=0; update(&f); assert(dropped(&f,p));
 vlan.idev.mc_list=&member; second.running=1; second.vid=4000;
 second.idev.mc_list=&member; update(&f);
 assert(f.group_count==2 && f.lut_count==IFC_COMMON_LUTS+4);
 p.vid=4000; assert(!dropped(&f,p));
 p.group++; assert(dropped(&f,p)); /* Full 32-bit group, not just MAC. */
 p.group=0xe0000001; assert(!dropped(&f,p));
 p.group=0xef030188; p.proto=2; assert(!dropped(&f,p));
 p.proto=17; p.ipv6=true; assert(!dropped(&f,p)); p.ipv6=false;
 p.ppp=true; assert(!dropped(&f,p)); p.ppp=false;
 p.qinq=true; assert(!dropped(&f,p)); p.qinq=false;
 p.group=0x0a000001; assert(!dropped(&f,p));
 p.group=0xef03017c; p.tagged=false; assert(dropped(&f,p));
 pon.idev.mc_list=&member; update(&f); assert(!dropped(&f,p));
 pon.idev.mc_list=NULL; update(&f); assert(dropped(&f,p));
 p.tagged=true; p.vid=0; assert(!dropped(&f,p)); /* Linux priority tag case. */
 p.vid=4001; p.group=0xef030188;
 vlan.bridge=1; update(&f); assert(!dropped(&f,p));
 assert(!strcmp(ifc_state(&f), "partial-bypass"));
 p.vid=4000; assert(dropped(&f,p)); /* Bypass only the unsupported scope. */
 vlan.bridge=0; vlan.route_error=-EOPNOTSUPP;
 update(&f); p.vid=4001; assert(!dropped(&f,p));
 vlan.route_error=0; pon.bridge=1; update(&f); assert(!f.active);
 assert(!strcmp(ifc_state(&f), "unsupported-scope"));
 pon.bridge=0; vlan.proto=htons(0x88a8); update(&f); assert(!f.active);
 vlan.proto=htons(ETH_P_8021Q); update(&f); assert(f.active);
 pon.upper=&vlan; vlan.net=1; update(&f); assert(!f.active);
 vlan.net=0; update(&f); assert(f.active); pon.upper=NULL;
 struct ip_mc_list groups[30]={0};
 for(unsigned i=0;i<30;i++) {
  groups[i].multiaddr=htonl(0xef010001+i);
  if(i+1<30) groups[i].next_rcu=&groups[i+1];
 }
 second.idev.mc_list=NULL; vlan.idev.mc_list=groups;
 update(&f); assert(!f.active && f.last_error==-ENOSPC && !f.broken);
 assert(!strcmp(ifc_state(&f), "capacity-exceeded"));
 assert(f.applied.generation < f.generation && f.applied.groups==2);
 ifc_changed(&f); assert(!strcmp(ifc_state(&f), "updating"));
 groups[26].next_rcu=NULL; update(&f); assert(f.active && f.lut_count==32);
 p.vid=4001; p.group=0xef01001b; assert(!dropped(&f,p));
 p.group++; assert(dropped(&f,p));
 race=1; update(&f); assert(!f.active && f.work.queued);
 assert(f.applied.generation < f.generation && !strcmp(ifc_state(&f), "updating"));
 race=0; ifc_work(&f.work); assert(f.active);
 program_error=-EIO; update(&f);
 assert(!f.active && f.broken && f.errors==1);
 assert(!strcmp(ifc_state(&f), "hardware-failure"));
 program_error=0; ifc_work(&f.work); assert(!f.active);
 struct seq_file seq={.private=&f};
 ifc_show(&seq,NULL);
 assert(strstr(seq.output,"state=hardware-failure"));
 assert(strstr(seq.output,"snapshot_current=0"));
 assert(strstr(seq.output,"hardware_drops=123"));
 puts("PASS: actual IFC assembly/snapshot/work; VLAN 4000 -> 4001, membership,");
 puts("      isolation, encapsulation, capacity and event/error fail-open handling");
}
'''
unit = prelude + types + registers
for name in ('ifc_gate_close', 'ifc_changed', 'ifc_add_group',
             'ifc_add_drop', 'ifc_lookup', 'ifc_scope_rules', 'ifc_snapshot'):
    unit += function(name)
unit += program + function('ifc_save_applied') + function('ifc_work') + function('ifc_state') + function('ifc_show') + tests
with tempfile.TemporaryDirectory(prefix='hg-ifc-policy-') as d:
    p = pathlib.Path(d)
    (p / 'test.c').write_text(unit)
    subprocess.run(['cc', '-std=gnu11', '-Wall', '-Wextra', '-Werror',
                    '-Wno-unused-but-set-variable', '-Wno-unused-parameter', str(p / 'test.c'),
                    '-o', str(p / 'test')], check=True)
    subprocess.run([str(p / 'test')], check=True)

igmp = (root / 'net/ipv4/igmp.c').read_text()
join = igmp.split('static void ____ip_mc_inc_group(', 1)[1].split('\nvoid ', 1)[0]
assert join.index('ipmr_offload_notify(') < join.index('igmp_group_added(')
assert 'ifc_changed(ifc);' in function('ifc_net_notify')
assert 'vlan_dev_vlan_id(dev)' in function('ifc_snapshot')
assert '4000' not in source and '4001' not in source
print('PASS: join-before-report ordering, netdev invalidation, no fixed service VID')

# Match the actual driver's one-based hardware ID to its zero-based array.
eth = (root / 'drivers/net/ethernet/airoha/airoha_eth.c').read_text()
gdm_alloc = eth.split('static int airoha_alloc_gdm_port(', 1)[1].split('\nstatic ', 1)[0]
assert 'p = id - 1;' in gdm_alloc and 'eth->ports[p] = port;' in gdm_alloc
assert 'eth->ports[AIROHA_GDM2_IDX - 1]' in source.split('void airoha_ifc_init(', 1)[1]
print('PASS: IFC uses the same GDM ID-to-array-index convention as allocation')
