#!/usr/bin/env python3
"""Run the actual HSGMII recovery code with controlled time/workqueue/MMIO."""
from pathlib import Path
import re
import subprocess
import sys
import tempfile

base = Path(sys.argv[1]) / 'drivers/net/pcs/airoha'
source = (base / 'pcs-airoha-recovery.c').read_text()
common = (base / 'pcs-airoha-common.c').read_text()
header = (base / 'pcs-airoha.h').read_text()
state = re.search(r'struct airoha_pcs_recovery \{.*?\n};', header, re.S)[0]
names = set(re.findall(r'\b(?:AIROHA|ARIOHA)_[A-Z0-9_]+\b', source))
defines = '\n'.join(m[0] for n in sorted(names)
                    if (m := re.search(r'^#define\s+' + n + r'\s+.+$', header, re.M)))
body = '\n'.join(line for line in source.splitlines()
                 if not line.startswith('#include'))
prelude = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stddef.h>
#include <stdio.h>
#include <string.h>
#include <limits.h>
#include <errno.h>
typedef uint32_t u32;
#define BIT(n) (1U << (n))
#define GENMASK(h,l) ((~0U >> (31-(h))) & (~0U << (l)))
#define FIELD_PREP(m,v) (((unsigned)(v) << __builtin_ctz(m)) & (m))
#define container_of(p,t,m) ((t *)((char *)(p) - offsetof(t,m)))
#define msecs_to_jiffies(ms) ((unsigned long)(ms))
#define time_before(a,b) ((long)((a)-(b)) < 0)
#define dev_info(...) ((void)0)
static unsigned warnings;
#define dev_warn_ratelimited(...) ((void)++warnings)
static unsigned long jiffies;
struct mutex { bool held; };
static void mutex_init(struct mutex *m) { m->held=false; }
static void mutex_lock(struct mutex *m) { assert(!m->held); m->held=true; }
static void mutex_unlock(struct mutex *m) { assert(m->held); m->held=false; }
static void unlock_guard(struct mutex **p) { mutex_unlock(*p); }
#define guard(type) struct mutex *__attribute__((cleanup(unlock_guard))) locked = lock_guard
static struct mutex *lock_guard(struct mutex *p) { mutex_lock(p); return p; }
struct work_struct { int unused; };
struct delayed_work { struct work_struct work; bool pending; unsigned long delay; };
#define to_delayed_work(p) container_of(p,struct delayed_work,work)
#define INIT_DELAYED_WORK(p,f) ((void)(f), memset((p),0,sizeof(*(p))))
static void *system_power_efficient_wq;
static void queue_delayed_work(void *q, struct delayed_work *w, unsigned long delay) {
    (void)q; w->pending=true; w->delay=delay;
}
#define mod_delayed_work queue_delayed_work
struct regmap { u32 r[0x1000/4]; int bank; };
struct airoha_pcs_maps { struct regmap *hsgmii_pcs, *multi_sgmii; };
struct airoha_pcs_priv { void *dev; struct airoha_pcs_maps maps[1]; struct regmap *pcs_pma[1]; };
'''
support = r'''
struct airoha_pcs_port {
    struct airoha_pcs_priv *priv; int index, config_error;
    struct mutex lock; struct delayed_work health_work;
    struct airoha_pcs_recovery recovery;
};
static void cancel_delayed_work_sync(struct delayed_work *w) {
    struct airoha_pcs_port *p=container_of(w,struct airoha_pcs_port,health_work);
    assert(!p->lock.held); w->pending=false;
}
static int devm_add_action_or_reset(void *dev, void (*fn)(void *), void *data) {
    (void)dev; (void)fn; (void)data; return 0;
}
static unsigned ops, reads, writes, fail_op, sleeps, positives=63, sample;
static struct airoha_pcs_port *current;
static int regmap_read(struct regmap *m, unsigned r, u32 *v) {
    assert(current->lock.held); assert(r/4 < 0x1000/4);
    if (++ops == fail_op) return -EIO;
    reads++;
    if (m->bank==1 && r==0x380) *v = ((positives >> (sample++ % 6)) & 1) << 8;
    else *v=m->r[r/4];
    return 0;
}
static int regmap_update_bits(struct regmap *m, unsigned r, u32 mask, u32 v) {
    assert(current->lock.held);
    /* New recovery code may write only these two lane-local PMA registers. */
    assert(m->bank==1 && (r==0x360 || r==0x818));
    if (++ops == fail_op) return -EIO;
    writes++; m->r[r/4]=(m->r[r/4]&~mask)|(v&mask); return 0;
}
static int regmap_set_bits(struct regmap *m, unsigned r, u32 mask) {
    return regmap_update_bits(m,r,mask,mask);
}
static int regmap_clear_bits(struct regmap *m, unsigned r, u32 mask) {
    return regmap_update_bits(m,r,mask,0);
}
static void usleep_range(unsigned a, unsigned b) {
    assert(current->lock.held);
    assert((a==700 && b==1000) || (a==100 && b==200)); sleeps++;
}
'''
checks = r'''
static struct regmap pma={.bank=1}, hsg, multi;
static struct airoha_pcs_priv priv={.pcs_pma={&pma}, .maps={{&hsg,&multi}}};
static struct airoha_pcs_port port={.priv=&priv};
static unsigned cases;
static void setup(void) {
    memset(&pma,0,sizeof(pma)); pma.bank=1;
    memset(&hsg,0,sizeof(hsg)); memset(&multi,0,sizeof(multi));
    memset(&port,0,sizeof(port)); port.priv=&priv; current=&port;
    assert(airoha_pcs_recovery_init(&port)==0);
    port.recovery.enabled=port.recovery.configured=port.recovery.link_requested=true;
    pma.r[AIROHA_PCS_PMA_RX_FREQDET/4]=AIROHA_PCS_PMA_FBCK_LOCK;
    pma.r[AIROHA_PCS_PMA_DIG_RESERVE_0/4]=0xa5a51234;
    pma.r[AIROHA_PCS_PMA_PXP_CDR_LPF_LCK_2DATA/4]=0x42424242;
    hsg.r[ARIOHA_PCS_HSGMII_PCS_STATE_2/4]=AIROHA_PCS_HSGMII_PCS_RX_SYNC;
    ops=reads=writes=fail_op=sleeps=sample=warnings=0; positives=63; jiffies=100;
}
static unsigned step(void) {
    mutex_lock(&port.lock);
    unsigned d=airoha_pcs_recovery_step(&port);
    mutex_unlock(&port.lock); cases++; return d;
}
int main(void) {
    setup(); assert(step()==1000 && reads==3 && writes==0);
    /* All disable combinations are read- and write-free. */
    for (unsigned mask=0;mask<16;mask++) {
        setup(); port.recovery.enabled=!!(mask&1); port.recovery.configured=!!(mask&2);
        port.recovery.link_requested=!!(mask&4); port.config_error=(mask&8)?-EIO:0;
        unsigned d=step();
        assert(d==((mask==7)?1000:0));
        if (mask!=7) assert(ops==0);
    }
    /* Transient failure and isolated FreqDet loss never trigger CDR writes. */
    setup(); hsg.r[ARIOHA_PCS_HSGMII_PCS_STATE_2/4]=0;
    assert(step()==100 && writes==0);
    hsg.r[ARIOHA_PCS_HSGMII_PCS_STATE_2/4]=32;
    assert(step()==1000 && writes==0 && !port.recovery.suspect);
    setup(); pma.r[AIROHA_PCS_PMA_RX_FREQDET/4]=0;
    assert(step()==100); assert(step()==1000 && writes==0);
    /* Six samples with every possible presence pattern; only >=4 may reset. */
    for (unsigned bits=0;bits<64;bits++) {
        setup(); positives=bits; hsg.r[ARIOHA_PCS_HSGMII_PCS_STATE_2/4]=0;
        assert(step()==100); unsigned d=step();
        assert(pma.r[AIROHA_PCS_PMA_DIG_RESERVE_0/4]==0xa5a51234);
        if (__builtin_popcount(bits)>=4) {
            assert(d==100 && sleeps==2 && port.recovery.verifying);
            hsg.r[ARIOHA_PCS_HSGMII_PCS_STATE_2/4]=32;
            assert(step()==1000 && !port.recovery.verifying && !port.recovery.attempts);
        } else assert(d==1000 && sleeps==0);
    }
    /* Three unsuccessful attempts, 1/2 second backoff then 30 second cooldown. */
    setup(); hsg.r[ARIOHA_PCS_HSGMII_PCS_STATE_2/4]=0;
    assert(step()==100);
    for (unsigned n=1;n<=3;n++) {
        assert(step()==100 && port.recovery.verifying);
        unsigned before=writes;
        assert(step()==1000 && !port.recovery.verifying);
        unsigned long delay=(n==3)?30000:(1000UL<<(n-1));
        assert(port.recovery.retry_at==jiffies+delay);
        jiffies+=delay-1; assert(step()==1000 && writes==before);
        jiffies++; /* next check is eligible, including cooldown expiry */
    }
    assert(step()==100 && port.recovery.attempts==1);
    /* All signal scan and relock operation failures are returned. */
    for (unsigned n=1;n<=9;n++) {
        setup(); fail_op=n; bool present=false; mutex_lock(&port.lock);
        assert(airoha_pcs_hsgmii_signal(&port,&present)==-EIO);
        mutex_unlock(&port.lock); cases++;
        if (n!=9) assert(pma.r[AIROHA_PCS_PMA_DIG_RESERVE_0/4]==0xa5a51234);
    }
    for (unsigned n=1;n<=7;n++) {
        setup(); fail_op=n; mutex_lock(&port.lock);
        assert(airoha_pcs_hsgmii_relock(&port)==-EIO);
        mutex_unlock(&port.lock); cases++;
        assert(pma.r[AIROHA_PCS_PMA_PXP_CDR_LPF_LCK_2DATA/4]==0x42424242);
    }
    setup(); pma.r[AIROHA_PCS_PMA_PXP_CDR_LPF_LCK_2DATA/4]|=BIT(24);
    mutex_lock(&port.lock); assert(airoha_pcs_hsgmii_relock(&port)==-EBUSY);
    mutex_unlock(&port.lock); assert(writes==0); cases++;
    for(unsigned n=1;n<=3;n++) {
        setup(); fail_op=n; assert(step()==1000 && sleeps==0 && writes==0);
        assert(port.recovery.last_error==-EIO);
    }
    setup(); hsg.r[ARIOHA_PCS_HSGMII_PCS_STATE_2/4]=0;
    assert(step()==100);
    for (unsigned n=0;n<3;n++) {
        fail_op=ops+4; assert(step()==1000);
        assert(warnings==1); /* persistent signal-read errors log one transition */
    }
    /* Tick wrapping, stop-before-run, reconfiguration and rearm. */
    setup(); port.recovery.suspect=true; port.recovery.waiting=true;
    jiffies=ULONG_MAX-100; port.recovery.retry_at=50;
    hsg.r[ARIOHA_PCS_HSGMII_PCS_STATE_2/4]=0;
    assert(step()==1000 && writes==0);
    jiffies=50; assert(step()==100 && port.recovery.verifying);
    setup(); mutex_lock(&port.lock); airoha_pcs_recovery_arm(&port); mutex_unlock(&port.lock);
    assert(port.health_work.pending && port.health_work.delay==100);
    airoha_pcs_recovery_stop(&port,false);
    assert(port.recovery.enabled && !port.recovery.configured && !port.health_work.pending);
    airoha_pcs_recovery_work(&port.health_work.work);
    assert(!port.health_work.pending && ops==0);
    port.recovery.configured=true;
    mutex_lock(&port.lock); airoha_pcs_recovery_arm(&port); mutex_unlock(&port.lock);
    assert(port.health_work.pending);
    airoha_pcs_recovery_release(&port);
    assert(!port.recovery.enabled && !port.health_work.pending);
    cases+=4;
    printf("PCS recovery: %u state/MMIO/lifecycle cases passed\n",cases);
}
'''
# Match data opt-in must be unique to AN7581 Ethernet; no PON/USB/PCIe/AN7583.
assert len(re.findall(r'\.hsgmii_rx_recovery\s*=\s*true', common)) == 1
eth = common.split('static const struct airoha_pcs_match_data an7581_pcs_eth = {')[1].split('};')[0]
assert '.hsgmii_rx_recovery = true' in eth
assert 'neg_mode != PHYLINK_PCS_NEG_INBAND_ENABLED' in common
with tempfile.TemporaryDirectory(prefix='hg-pcs-recovery-') as d:
    c, exe = Path(d)/'test.c', Path(d)/'test'
    c.write_text(prelude + state + support + defines + '\n' + body + '\n' + checks)
    subprocess.run(['cc', '-std=gnu11', '-Wall', '-Wextra', '-Werror',
                    str(c), '-o', str(exe)], check=True)
    subprocess.run([str(exe)], check=True)
