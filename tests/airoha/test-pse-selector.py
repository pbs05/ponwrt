#!/usr/bin/env python3
"""Exercise the real PSE queue query with simulated indirect MMIO."""
from pathlib import Path
import re
import subprocess
import sys
import tempfile

base = Path(sys.argv[1]) / 'drivers/net/ethernet/airoha'
eth = (base / 'airoha_eth.c').read_text()
regs = (base / 'airoha_regs.h').read_text()
start = eth.index('static u32 airoha_fe_get_pse_queue_rsv_pages(')
function = eth[start:eth.index('\n}', start) + 2]
names = ('REG_FE_PSE_QUEUE_CFG_WR', 'REG_FE_PSE_QUEUE_CFG_VAL', 'PSE_CFG_PORT_ID_MASK',
         'PSE_CFG_QUEUE_ID_MASK', 'PSE_CFG_WR_EN_MASK', 'PSE_CFG_OQ_RSV_MASK')
defines = '\n'.join(re.search(r'^#define\s+' + name + r'\s+.+$', regs, re.M)[0] for name in names)
prelude = r'''
#include <assert.h>
#include <stdint.h>
#include <stdio.h>
typedef uint32_t u32;
#define BIT(n) (1U<<(n))
#define GENMASK(h,l) ((~0U>>(31-(h)))&(~0U<<(l)))
#define FIELD_PREP(m,v) (((v)<<__builtin_ctz(m))&(m))
#define FIELD_GET(m,v) (((v)&(m))>>__builtin_ctz(m))
struct airoha_eth { u32 selector, reservations[11][32], total, writes; };
'''
mmio = r'''
static void airoha_fe_wr(struct airoha_eth *e,u32 reg,u32 value) {
    assert(reg==REG_FE_PSE_QUEUE_CFG_WR);
    assert(!(value&PSE_CFG_WR_EN_MASK));
    assert(!(value&~(PSE_CFG_PORT_ID_MASK|PSE_CFG_QUEUE_ID_MASK)));
    e->selector=value;e->writes++;
}
static u32 airoha_fe_get(struct airoha_eth *e,u32 reg,u32 mask) {
    assert(reg==REG_FE_PSE_QUEUE_CFG_VAL && mask==PSE_CFG_OQ_RSV_MASK);
    return e->reservations[FIELD_GET(PSE_CFG_PORT_ID_MASK,e->selector)]
                          [FIELD_GET(PSE_CFG_QUEUE_ID_MASK,e->selector)];
}
'''
test = r'''
int main(void) {
    static const unsigned queues[]={6,6,32,6,4,6,8,10,4,2,2};
    struct airoha_eth e={.total=0x10e0};unsigned cases=0;
    for(unsigned p=0;p<11;p++) for(unsigned q=0;q<queues[p];q++) e.reservations[p][q]=p*32+q;
    for(unsigned old=0;old<4;old++) for(unsigned p=0;p<11;p++) for(unsigned q=0;q<queues[p];q++) {
        e.selector=old==0?0:old==1?0xffffffff:old==2?PSE_CFG_WR_EN_MASK:0x0a010001;
        assert(airoha_fe_get_pse_queue_rsv_pages(&e,p,q)==p*32+q);
        assert(e.total==0x10e0);cases++;
    }
    assert(cases==344 && e.writes==cases);
    for(unsigned p=0;p<11;p++) for(unsigned q=0;q<queues[p];q++) assert(e.reservations[p][q]==p*32+q);
    printf("PASS: %u real PSE queries; no configuration writes or command replay\n",cases);
}
'''
with tempfile.TemporaryDirectory(prefix='hg-pse-selector-') as tmp:
    src=Path(tmp)/'selector.c';exe=Path(tmp)/'selector'
    src.write_text(prelude+defines+'\n'+mmio+function+'\n'+test)
    subprocess.run(['cc','-std=gnu11','-Wall','-Wextra','-Werror',str(src),'-o',str(exe)],check=True)
    subprocess.run([str(exe)],check=True)
