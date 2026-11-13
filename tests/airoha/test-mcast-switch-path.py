#!/usr/bin/env python3
"""Compile actual SDK-path selection, cache publication and fault functions."""
from pathlib import Path
import re
import subprocess
import sys
import tempfile

source=Path(sys.argv[1])/'drivers/net/ethernet/airoha/airoha_mcast.c'
text=source.read_text()


def function(name):
    start=re.search(r'^static (?:bool|void|int|u32) '+name+r'\(',text,re.M).start()
    end=text.index('{',start)+1;depth=1
    while depth:
        depth+=(text[end]=='{')-(text[end]=='}');end+=1
    return text[start:end]+'\n'


def run(name,code):
    with tempfile.TemporaryDirectory(prefix='hg-switch-'+name+'-') as tmp:
        c=Path(tmp)/'test.c';exe=Path(tmp)/'test'
        c.write_text(code)
        subprocess.run(['cc','-std=gnu11','-O2','-Wall','-Wextra','-Werror',str(c),'-o',str(exe)],check=True)
        subprocess.run([str(exe)],check=True)


prefix=r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <errno.h>
typedef uint32_t u32;
typedef uint64_t u64;
#define BIT(n) (1U<<(n))
#define GENMASK(h,l) ((~0U>>(31-(h)))&(~0U<<(l)))
#define FIELD_PREP(m,v) (((u32)(v)<<__builtin_ctz(m))&(m))
#define MC_DSA_COUNT 6
#define MC_DSA_PORTS GENMASK(5,0)
#define MC_NATIVE_SLOT 13
#define MC_OUTPUTS (MC_DSA_PORTS|BIT(MC_NATIVE_SLOT))
#define MC_MAX_FLOWS 4
#define READ_ONCE(x) (x)
static void spin_lock_bh(int *p) { assert(!*p);*p=1; }
static void spin_unlock_bh(int *p) { assert(*p);*p=0; }
#define lockdep_assert_held(p) assert(*(p))
'''
link=re.search(r'^struct mc_link \{.*?^};',text,re.M|re.S)[0]+'\n'
cache=r'''
struct ethtool_link_ksettings { struct { u32 speed; } base; };
struct ethtool_ops { void *get_link_ksettings; };
struct airoha_port { int id; };
struct airoha_gdm_dev { struct airoha_port *port; bool foreign; };
struct net_device;
struct dsa_port { int index; struct net_device *conduit; };
struct net_device {
 struct net_device *next;
 bool dsa,running,carrier;
 int ifindex;
 u32 speed;
 struct ethtool_ops *ethtool_ops;
 struct dsa_port *dp;
 struct airoha_gdm_dev *gdm;
};
struct airoha_eth { int unused; };
struct airoha_ppe { struct airoha_eth *eth; };
struct mc_flow {
 u32 outputs;bool switch_path,used,committed;
 unsigned char channel,tx_channels[MC_DSA_COUNT];
 int output_ifindex[MC_DSA_COUNT];
};
struct airoha_mcast {
 int lock;bool links_dirty,enabled,stopping;
 u64 epoch;struct mc_link links[MC_DSA_COUNT];
 struct airoha_ppe *ppe;
};
static struct airoha_mcast *current;
static struct net_device *devices;
static int init_net, queries, invalidated;
static bool rtnl_available,rtnl_held,race;
#define AIROHA_GDM1_IDX 1
#define SPEED_UNKNOWN UINT32_MAX
#define MC_EVENT_NETDEV 6
#define for_each_netdev(net,dev) for((void)(net),dev=devices;dev;dev=dev->next)
#define dsa_user_dev_check(d) ((d)->dsa)
#define netif_running(d) ((d)->running)
#define netif_carrier_ok(d) ((d)->carrier)
#define dsa_port_from_netdev(d) ((d)->dp)
#define dsa_port_to_conduit(dp) ((dp)->conduit)
#define netdev_priv(d) ((d)->gdm)
static bool rtnl_trylock(void) { assert(!rtnl_held);rtnl_held=rtnl_available;return rtnl_held; }
static void rtnl_unlock(void) { assert(rtnl_held);rtnl_held=false; }
static bool airoha_is_valid_gdm_dev(struct airoha_eth *e,struct airoha_gdm_dev *gdm) {
 (void)e;return !gdm->foreign;
}
static int __ethtool_get_link_ksettings(struct net_device *d,struct ethtool_link_ksettings *s) {
 assert(rtnl_held&&!current->lock);queries++;
 s->base.speed=d->speed;
 if(race)current->epoch++;
 return 0;
}
static void mc_invalidate_locked(struct airoha_mcast *m,int why,void *filter) {
 assert(m->lock&&why==MC_EVENT_NETDEV&&!filter);m->epoch++;invalidated++;
}
'''
checks=r'''
int main(void) {
 struct airoha_eth eth={0};struct airoha_ppe ppe={.eth=&eth};
 struct airoha_mcast mc={.enabled=true,.links_dirty=true,.ppe=&ppe};
 struct airoha_port port={.id=1};struct airoha_gdm_dev gdm={.port=&port};
 struct net_device conduit={.gdm=&gdm};
 struct ethtool_ops ops={.get_link_ksettings=(void*)1};
 struct dsa_port dp={.index=0,.conduit=&conduit};
 struct net_device dev={.dsa=true,.running=true,.carrier=true,.ifindex=10,
                        .speed=1000,.dp=&dp,.ethtool_ops=&ops};
 devices=&dev;current=&mc;
 mc_refresh_links(&mc);assert(mc.links_dirty&&queries==0&&!rtnl_held);
 rtnl_available=true;mc_refresh_links(&mc);
 assert(!mc.links_dirty&&mc.links[0].speed==1000&&mc.links[0].ifindex==10);
 assert(invalidated==1&&!rtnl_held);
 mc.links_dirty=true;mc_refresh_links(&mc);assert(invalidated==1&&!mc.links_dirty);
 race=true;mc.links_dirty=true;dev.speed=100;
 mc_refresh_links(&mc);assert(mc.links_dirty&&mc.links[0].speed==1000&&!rtnl_held);
 race=false;mc_refresh_links(&mc);assert(!mc.links_dirty&&mc.links[0].speed==100&&invalidated==2);
 dev.carrier=false;mc.links_dirty=true;mc_refresh_links(&mc);assert(!mc.links[0].speed);
 mc.stopping=true;mc.links_dirty=true;int before=queries;
 mc_refresh_links(&mc);assert(queries==before&&mc.links_dirty&&!rtnl_held);
 mc.stopping=false;mc.links_dirty=false;mc.lock=1;
 struct mc_flow f={0};
 for(unsigned i=0;i<MC_DSA_COUNT;i++) {
  mc.links[i]=(struct mc_link){.ifindex=10+i,.speed=1000};
  f.output_ifindex[i]=10+i;f.tx_channels[i]=20+i;
  f.outputs=BIT(i);mc_select_path(&mc,&f);assert(f.switch_path&&f.channel==20+i);
 }
 f.outputs=BIT(0)|BIT(2)|BIT(5);mc.links[2].speed=2500;
 mc_select_path(&mc,&f);assert(f.switch_path&&f.channel==22);
 mc.links[2].speed=1000;mc_select_path(&mc,&f);assert(f.channel==20);
 mc.links[0].speed=100;mc_select_path(&mc,&f);assert(f.channel==22);
 mc.links[2].speed=0;mc_select_path(&mc,&f);assert(!f.switch_path);
 mc.links[2].speed=1000;f.output_ifindex[2]++;
 mc_select_path(&mc,&f);assert(!f.switch_path);
 f.output_ifindex[2]--;mc.links_dirty=true;mc_select_path(&mc,&f);assert(!f.switch_path);
 mc.links_dirty=false;f.outputs|=BIT(MC_NATIVE_SLOT);
 mc_select_path(&mc,&f);assert(!f.switch_path);
 f.outputs=0;mc_select_path(&mc,&f);assert(!f.switch_path);
 mc.lock=0;
 puts("PASS: actual SDK selector/cache; all ports, speed/ties, identity, RTNL and epoch races");
}
'''
run('selector',prefix+link+cache+function('mc_refresh_links')+function('mc_select_path')+checks)

fault=r'''
struct airoha_foe_entry { u32 ib1,data[19]; };
struct mc_flow { bool used,committed,switch_path;int hash;void *mfc;struct airoha_foe_entry bound; };
struct airoha_qdma { u32 slots[16]; };
struct soc { int num_ppe; };
struct airoha_eth { struct airoha_qdma qdma[1];struct soc *soc;u32 enable[2]; };
struct airoha_ppe { struct airoha_eth *eth; };
struct airoha_mcast {
 int lock;struct airoha_ppe *ppe;struct mc_flow flows[MC_MAX_FLOWS];
 bool slots_owned,running,broken,isolated,ppe_stopped;
 unsigned errors,invalidated;
};
static int write_error,puts_count;
static bool readback_failure;
#define FE_PSE_PORT_DROP 15
#define MC_REPL_FPORT GENMASK(24,16)
#define MC_REPL_REG(n) (n)
#define PPE_GLO_CFG_EN_MASK BIT(0)
#define REG_PPE_GLO_CFG(n) (n)
#define AIROHA_FOE_IB1_BIND_STATE GENMASK(29,28)
#define dev_err_once(...) ((void)0)
static void airoha_qdma_wr(struct airoha_qdma *q,u32 reg,u32 val) { assert(reg<16);q->slots[reg]=val; }
static u32 airoha_qdma_rr(struct airoha_qdma *q,u32 reg) { return readback_failure?0:q->slots[reg]; }
static void airoha_fe_clear(struct airoha_eth *e,u32 reg,u32 mask) { e->enable[reg]&=~mask; }
static int airoha_ppe_mcast_write(struct airoha_ppe *p,int hash,const struct airoha_foe_entry *old,
                                 const struct airoha_foe_entry *next) {
 (void)p;(void)hash;assert(!(next->ib1&AIROHA_FOE_IB1_BIND_STATE));
 assert((old->ib1&AIROHA_FOE_IB1_BIND_STATE));return write_error;
}
static void mr_cache_put(void *m) { assert(m);puts_count++; }
'''
fault_checks=r'''
int main(void) {
 struct soc soc={.num_ppe=2};struct airoha_eth eth={.soc=&soc};
 struct airoha_ppe ppe={.eth=&eth};
 for(int direct=0;direct<2;direct++) for(int pending=0;pending<2;pending++) {
  struct airoha_mcast mc={.ppe=&ppe,.slots_owned=true,.running=true};
  eth.enable[0]=eth.enable[1]=1;
  mc.flows[0]=(struct mc_flow){.used=true,.committed=!pending,.switch_path=direct};
  mc_fault(&mc);
  assert(mc.broken&&!mc.running&&mc.errors==1);
  assert(mc.isolated==!direct&&mc.ppe_stopped==direct);
  assert(eth.enable[0]==!direct&&eth.enable[1]==!direct);
 }
 for(int err=0;err<3;err++) {
  struct airoha_mcast mc={.ppe=&ppe,.slots_owned=true,.running=true};
  struct mc_flow *f=&mc.flows[0];
  *f=(struct mc_flow){.used=true,.committed=true,.switch_path=true,.mfc=&soc};
  f->bound.ib1=2U<<28;eth.enable[0]=eth.enable[1]=1;
  write_error=err==0?0:err==1?-ESTALE:-ETIMEDOUT;
  mc_remove(&mc,f);
  assert(!f->used&&mc.invalidated==1);
  assert(mc.ppe_stopped==(err==2));
  assert(mc.broken==(err==2));
 }
 assert(puts_count==3);
 struct airoha_mcast mc={.ppe=&ppe,.slots_owned=true,.running=true};
 readback_failure=true;mc_fault(&mc);assert(!mc.isolated&&mc.ppe_stopped);
 puts("PASS: actual fault/revoke functions; pending direct timeout, replica isolation, ESTALE and failure");
}
'''
# The fault helpers themselves are called with the existing state lock held.
run('fault',prefix.replace('static void spin_lock_bh', 'static __attribute__((unused)) void spin_lock_bh')
    .replace('static void spin_unlock_bh','static __attribute__((unused)) void spin_unlock_bh')
    +fault+function('mc_fault')+function('mc_remove')+fault_checks)

reap=function('mc_reap')
assert reap.index('mc_refresh_links(mc)')<reap.index('spin_lock_bh(&mc->lock)')
assert 'rtnl_lock()' not in function('mc_refresh_links')
tx=text[text.index('void airoha_mcast_tx('):text.index('bool airoha_mcast_rx_copy(')]
assert tx.index('if (f->seen != f->outputs)')<tx.index('mc_select_path(mc, f)')
assert tx.index('mc_select_path(mc, f)')<tx.index('mc_encode(f)')<tx.index('airoha_ppe_mcast_write(')
print('PASS: source integration; cache query outside state lock, selection only after all TX submissions')
