#!/usr/bin/env python3
"""Exercise the actual route-miss cache with host locks/time, no devices."""
import pathlib
import re
import subprocess
import sys
import tempfile

source = pathlib.Path(sys.argv[1])
text = (source / 'drivers/net/ethernet/airoha/airoha_mcast.c').read_text()


def function(name):
    match = re.search(r'^static (?:bool|void) ' + name + r'\(', text, re.M)
    start = match.start()
    end = text.index('{', start) + 1
    depth = 1
    while depth:
        depth += (text[end] == '{') - (text[end] == '}')
        end += 1
    return text[start:end] + '\n'


def declaration(name):
    return re.search(r'^struct ' + name + r' \{.*?^};', text, re.M | re.S)[0]


prelude = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
typedef uint64_t u64;
typedef uint32_t __be32;
typedef uint16_t u16, __be16;
typedef uint8_t u8;
#define HZ 100
#define MC_RETRY_SLOTS 64
#define MC_RETRY_LIFE HZ
#define time_before(a,b) ((long)((a)-(b)) < 0)
static unsigned long jiffies;
static void spin_lock_bh(int *lock) { assert(!*lock); *lock = 1; }
static void spin_unlock_bh(int *lock) { assert(*lock); *lock = 0; }
#define lockdep_assert_held(lock) assert(*(lock))
'''
types = r'''
struct mc_flow { struct mc_packet sample; u16 hash; int iif; };
struct airoha_mcast {
 int lock; bool running; u64 epoch, retry_skipped;
 struct mc_retry retry[MC_RETRY_SLOTS];
};
'''
test = r'''
static void defer(struct airoha_mcast *mc, struct mc_flow *f, u64 epoch) {
 spin_lock_bh(&mc->lock); mc_retry_defer(mc, f, epoch); spin_unlock_bh(&mc->lock);
}
int main(void) {
 struct airoha_mcast mc = {.running=true};
 struct mc_flow f = {.hash=7, .iif=14,
  .sample={.source=1,.group=2,.sport=3,.dport=4,.tagged=true,.vlan=4000,.ttl=124}};
 struct mc_flow other;
 u64 epoch;
 assert(!mc_retry_skip(&mc, &f, &epoch));
 defer(&mc, &f, epoch);
 for(jiffies=10;jiffies<100;jiffies+=10) assert(mc_retry_skip(&mc,&f,&epoch));
 assert(mc.retry_skipped==9);
 assert(!mc_retry_skip(&mc,&f,&epoch)); /* Fixed one second, never sliding. */
 defer(&mc,&f,epoch); mc.epoch++; /* Join/route event immediately permits retry. */
 assert(!mc_retry_skip(&mc,&f,&epoch));
 defer(&mc,&f,epoch-1); assert(!mc_retry_skip(&mc,&f,&epoch)); /* Race. */
 defer(&mc,&f,epoch);
#define DIFFERENT(field,value) other=f; other.field=value; assert(!mc_retry_skip(&mc,&other,&epoch))
 DIFFERENT(hash,71); /* Same cache slot, different hardware hash. */
 DIFFERENT(iif,15); DIFFERENT(sample.source,99); DIFFERENT(sample.group,99);
 DIFFERENT(sample.sport,99); DIFFERENT(sample.dport,99);
 DIFFERENT(sample.tagged,false); DIFFERENT(sample.vlan,10);
 DIFFERENT(sample.dsfield,32); DIFFERENT(sample.ttl,125);
 other=f; other.sample.id=42; other.sample.length=1500;
 assert(mc_retry_skip(&mc,&other,&epoch)); /* Packet IDs do not defeat cache. */
 other=f; other.hash+=64; defer(&mc,&other,epoch);
 assert(!mc_retry_skip(&mc,&f,&epoch));
 assert(mc_retry_skip(&mc,&other,&epoch)); /* Collision only loses optimization. */
 mc.epoch++; mc.running=false; defer(&mc,&f,mc.epoch);
 assert(!mc_retry_skip(&mc,&f,&epoch));
 mc.running=true; jiffies=~0UL-49; defer(&mc,&f,epoch);
 jiffies=0; assert(mc_retry_skip(&mc,&f,&epoch));
 jiffies=50; assert(!mc_retry_skip(&mc,&f,&epoch)); /* jiffies wrap. */
 puts("PASS: real retry functions; expiry, events, races, full key and wrap");
}
'''
unit = prelude + declaration('mc_packet') + declaration('mc_retry') + types
unit += ''.join(function(n) for n in ('mc_same_tuple', 'mc_retry_skip', 'mc_retry_defer')) + test
with tempfile.TemporaryDirectory(prefix='hg-mcast-retry-') as d:
    c = pathlib.Path(d) / 'test.c'
    binary = pathlib.Path(d) / 'test'
    c.write_text(unit)
    subprocess.run(['cc', '-std=gnu11', '-Wall', '-Wextra', '-Werror', '-o', str(binary), str(c)], check=True)
    subprocess.run([str(binary)], check=True)

learn = text.split('void airoha_mcast_learn(', 1)[1].split('\nstatic ', 1)[0]
assert learn.index('in != READ_ONCE(mc->ingress)') < learn.index('mc_retry_skip(')
assert learn.index('mc_retry_skip(') < learn.index('ipmr_offload_snapshot(')
assert 'if (why == AIROHA_MC_REJECT_ROUTE)\n\t\tmc_retry_defer(' in learn
assert 'mc->epoch++;' in function('mc_invalidate_locked')
print('PASS: scope/admission/event integration; no packet drop or BIND on a cached miss')
