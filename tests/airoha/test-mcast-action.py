#!/usr/bin/env python3
"""Compile the real multicast encoder and FOE layouts, without hardware.

--expect-dirty is a negative control for the pre-942-12 encoder. It is not a
hardware model and cannot establish the cause of PSE buffer exhaustion.
"""
import argparse
from pathlib import Path
import re
import subprocess
import tempfile

p = argparse.ArgumentParser(description=__doc__)
p.add_argument('source', type=Path)
p.add_argument('--expect-dirty', action='store_true')
a = p.parse_args()
base = a.source / 'drivers/net/ethernet/airoha'
header = (base / 'airoha_eth.h').read_text()
text = (base / 'airoha_mcast.c').read_text()


def function(name):
    start = re.search(r'^static (?:void|int|bool|u32) ' + name + r'\(', text, re.M).start()
    end = text.index('{', start) + 1
    depth = 1
    while depth:
        depth += (text[end] == '{') - (text[end] == '}')
        end += 1
    return text[start:end] + '\n'


layouts = header[header.index('struct airoha_foe_mac_info_common {'):
                 header.index('\nstruct airoha_foe_stats {')]
packet = re.search(r'^struct mc_packet \{.*?^};', text, re.M | re.S)[0]
prelude = r'''
#include <assert.h>
#include <arpa/inet.h>
#include <stdbool.h>
#include <stdint.h>
#include <stddef.h>
#include <stdio.h>
#include <string.h>
typedef uint8_t u8;
typedef uint16_t u16, __be16;
typedef uint32_t u32, __be32;
#define BIT(n) (1U << (n))
#define GENMASK(h,l) ((~0U >> (31-(h))) & (~0U << (l)))
#define FIELD_PREP(m,v) (((u32)(v) << __builtin_ctz(m)) & (m))
#define FIELD_GET(m,v) (((v) & (m)) >> __builtin_ctz(m))
#define DECLARE_FLEX_ARRAY(t,n) t n[0]
#define static_assert _Static_assert
#define PPE_ENTRY_SIZE 80
#define ETH_ALEN 6
#define AIROHA_FOE_STATE_BIND 2
#define MC_FPORT 14
#define FE_PSE_PORT_GDM1 1
#define MC_DSA_PORTS GENMASK(5,0)
#define AIROHA_STAG_FUP BIT(15)
#define AIROHA_STAG_UP GENMASK(14,12)
static u16 airoha_stag_set_priority(u16 tag, u8 queue) {
    return (tag & ~AIROHA_STAG_UP) | AIROHA_STAG_FUP | FIELD_PREP(AIROHA_STAG_UP,queue);
}
static u32 get_unaligned_be32(const void *p) { u32 v; memcpy(&v,p,4); return ntohl(v); }
static u16 get_unaligned_be16(const void *p) { u16 v; memcpy(&v,p,2); return ntohs(v); }
static void ip_eth_mc_map(__be32 group, u8 *mac) {
    u32 g=ntohl(group); mac[0]=1; mac[1]=0; mac[2]=0x5e;
    mac[3]=(g>>16)&0x7f; mac[4]=g>>8; mac[5]=g;
}
'''
flow = r'''
struct mc_flow {
    struct airoha_foe_entry learned, bound;
    struct mc_packet sample;
    u16 outputs, out_vlan;
    u8 source_mac[ETH_ALEN], dsfield, queue, channel;
    bool out_tagged, local, switch_path;
};
'''
test = r'''
int main(void) {
    unsigned checks=0, dirty=0;
    static const u32 noise[]={0,0xffffffff,0xa5a5a5a5,0xfec2a28c,0x01bbbc6c};
    for(unsigned q=0;q<8;q++) for(unsigned ds=0;ds<256;ds++)
    for(unsigned local=0;local<2;local++) for(unsigned tagged=0;tagged<2;tagged++)
    for(unsigned path=0;path<(HAS_SWITCH_PATH?2U:1U);path++) {
        struct mc_flow f={.outputs=BIT(2)|BIT(4),.queue=q,.dsfield=ds,
                         .local=local,.out_tagged=tagged,.out_vlan=tagged?4001:0,
                         .switch_path=path,.channel=4,
                         .source_mac={2,3,4,5,6,7},
                         .sample={.group=htonl(0xef010203),.sport=htons(8046),.dport=htons(2000)}};
        struct airoha_foe_entry expected={0};
        f.learned.ipv4.orig_tuple=(struct airoha_foe_ipv4_tuple){
            .src_ip=0xc6336401,.dest_ip=0xef010203,.ports=0xa5a10fa0};
        for(unsigned n=0;n<sizeof(noise)/sizeof(noise[0]);n++) {
            for(unsigned i=16;i<sizeof(f.learned);i+=4) memcpy(f.learned.data+i,&noise[n],4);
            memset(&f.bound,0xcc,sizeof(f.bound));
            mc_encode(&f);
            assert(!memcmp(&f.bound.ipv4.orig_tuple,&f.learned.ipv4.orig_tuple,12));
            assert(f.bound.ipv4.new_tuple.src_port==8046 && f.bound.ipv4.new_tuple.dest_port==2000);
            assert(FIELD_GET(AIROHA_FOE_IB2_DSCP,f.bound.ipv4.ib2)==ds);
            assert(FIELD_GET(AIROHA_FOE_QID,f.bound.ipv4.data)==q);
            assert(FIELD_GET(AIROHA_FOE_IB2_PSE_PORT,f.bound.ipv4.ib2)==(path?1:14));
            assert(FIELD_GET(AIROHA_FOE_IB2_NBQ,f.bound.ipv4.ib2)==(path?1:0));
            assert(FIELD_GET(AIROHA_FOE_CHANNEL,f.bound.ipv4.data)==(path?4:0));
            assert(!(f.bound.ipv4.ib2&AIROHA_FOE_IB2_FAST_PATH));
            assert(!!(f.bound.ipv4.ib2&AIROHA_FOE_IB2_MULTICAST)==local);
            assert(FIELD_GET(AIROHA_FOE_IB1_BIND_VLAN_LAYER,f.bound.ib1)==tagged);
            assert(f.bound.ipv4.l2.common.vlan1==(tagged?4001:0));
            assert((f.bound.ipv4.l2.common.etype&MC_DSA_PORTS)==f.outputs);
            assert(FIELD_GET(AIROHA_STAG_UP,f.bound.ipv4.l2.common.etype)==q);
            if(!n) expected=f.bound;
            if(f.bound.ipv4.rsv[0]||f.bound.ipv4.rsv[1]) dirty++;
            if(!EXPECT_DIRTY) {
                assert(!f.bound.ipv4.rsv[0]&&!f.bound.ipv4.rsv[1]);
                assert(!memcmp(&expected,&f.bound,sizeof(expected)));
            }
            checks++;
        }
    }
    assert(EXPECT_DIRTY ? dirty>0 : dirty==0);
    printf("PASS: %u real-encoder cases; dirty=%u; negative-control=%u\n",checks,dirty,EXPECT_DIRTY);
}
'''
with tempfile.TemporaryDirectory(prefix='hg-mcast-action-') as tmp:
    src = Path(tmp) / 'action.c'
    exe = Path(tmp) / 'action'
    encoder = function('mc_encode')
    capability = '#define HAS_SWITCH_PATH ' + str(int('f->switch_path ?' in encoder)) + '\n'
    src.write_text(prelude + layouts + '\n' + packet + '\n' + flow + capability + encoder + test)
    subprocess.run(['cc', '-std=gnu11', '-O2', '-Wall', '-Wextra', '-Werror',
                    '-DEXPECT_DIRTY=' + str(int(a.expect_dirty)), str(src), '-o', str(exe)], check=True)
    subprocess.run([str(exe)], check=True)
