#!/usr/bin/env python3
"""Compile the real LAN congestion initializer against bounded fake MMIO."""
from pathlib import Path
import re
import subprocess
import sys
import tempfile

base = Path(sys.argv[1]) / 'drivers/net/ethernet/airoha'
source = (base / 'airoha_eth.c').read_text()
regs = (base / 'airoha_regs.h').read_text()
start = source.index('static void airoha_qdma_init_lan_sram_congestion(')
function = source[start:source.index('\nstatic void ', start + 1)]
names = (
    'REG_LMGR_INIT_CFG', 'LMGR_SRAM_MODE_MASK', 'HW_FWD_DESC_NUM_MASK',
    'REG_TXQ_CNGST_CFG', 'TXQ_CNGST_DROP_EN', 'TXQ_CNGST_DEI_DROP_EN',
    'TXQ_CNGST_DYN_EN', 'TXQ_CNGST_CFG_TRIGGER', 'TXQ_CNGST_PACKET_TRIGGER',
    'TXQ_CNGST_TIME_TRIGGER', 'TXQ_CNGST_TICKSEL_MASK',
    'REG_TXQ_DYN_TOTAL_THR', 'REG_TXQ_DYN_CHAN_THR', 'REG_TXQ_DYN_QUEUE_THR',
    'REG_TXQ_DEI_TOTAL_THR', 'REG_TXQ_DEI_CHAN_THR', 'REG_TXQ_DEI_QUEUE_THR',
    'TXQ_CNGST_MAX_THR_MASK', 'TXQ_CNGST_MIN_THR_MASK',
)
defines = '\n'.join(re.search(r'^#define\s+' + name + r'\s+.+$', regs, re.M)[0]
                    for name in names)
prelude = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
typedef uint32_t u32;
typedef uint16_t u16;
#define BIT(n) (1U << (n))
#define GENMASK(h, l) ((~0U >> (31 - (h))) & (~0U << (l)))
#define FIELD_PREP(m, v) (((u32)(v) << __builtin_ctz(m)) & (m))
#define ARRAY_SIZE(a) ((int)(sizeof(a) / sizeof((a)[0])))
struct airoha_eth;
struct airoha_qdma {
    struct airoha_eth *eth;
    u32 regs[0x1200 / 4];
    u32 writes[8], count;
};
struct airoha_eth { struct airoha_qdma qdma[2]; bool an7581; };
static bool airoha_is_7581(struct airoha_eth *e) { return e->an7581; }
static u32 airoha_qdma_rr(struct airoha_qdma *q, u32 reg) {
    assert(!(reg & 3) && reg < sizeof(q->regs));
    return q->regs[reg / 4];
}
'''
mmio = r'''
static void airoha_qdma_wr(struct airoha_qdma *q, u32 reg, u32 value) {
    assert(!(reg & 3) && reg < sizeof(q->regs) && q->count < 8);
    q->writes[q->count++] = reg;
    q->regs[reg / 4] = value;
}
static void airoha_qdma_rmw(struct airoha_qdma *q, u32 reg, u32 mask, u32 value) {
    assert(reg == REG_TXQ_CNGST_CFG);
    airoha_qdma_wr(q, reg, (airoha_qdma_rr(q, reg) & ~mask) | value);
}
'''
test = r'''
int main(void) {
    static const u32 counts[] = {0, 1, 256, 2048, 8192, 16383, 16384,
                                16385, 32768, 65535, 65536, 131071};
    static const u32 seeds[] = {0, 0xffffffff, 0xa5a5a5a5, 0xe00700fa, 0x900fbeef};
    static const u32 offsets[] = {0x10a4, 0x10a8, 0x10ac, 0x10d4, 0x10d8, 0x10dc};
    static const u32 values[] = {0x3ff03f00, 0x3ff00400, 0x20000020,
                                 0x1ff81f80, 0x1ff80200, 0x10000010};
    const u32 owned = 0x20070000 | 0x0000ffff;
    unsigned cases = 0, supported = 0;
    for (int chip = 0; chip < 2; chip++)
    for (int block = 0; block < 2; block++)
    for (int sram = 0; sram < 2; sram++)
    for (unsigned c = 0; c < sizeof(counts) / sizeof(counts[0]); c++)
    for (unsigned s = 0; s < sizeof(seeds) / sizeof(seeds[0]); s++) {
        struct airoha_eth e = {.an7581 = chip};
        struct airoha_qdma *q = &e.qdma[block];
        q->eth = &e;
        for (unsigned r = 0; r < sizeof(q->regs) / sizeof(q->regs[0]); r++)
            q->regs[r] = seeds[s] ^ r;
        q->regs[REG_LMGR_INIT_CFG / 4] = (sram ? LMGR_SRAM_MODE_MASK : 0) |
            FIELD_PREP(HW_FWD_DESC_NUM_MASK, counts[c]) | 0x11800000;
        q->regs[REG_TXQ_CNGST_CFG / 4] = seeds[s];
        u32 before[sizeof(q->regs) / sizeof(q->regs[0])];
        memcpy(before, q->regs, sizeof(before));
        airoha_qdma_init_lan_sram_congestion(q);
        bool applicable = chip && block == 0 && sram && counts[c] == 16384;
        if (!applicable) {
            assert(q->count == 0);
            assert(!memcmp(before, q->regs, sizeof(before)));
        } else {
            supported++;
            assert(q->count == 7);
            assert(q->writes[6] == REG_TXQ_CNGST_CFG);
            for (int i = 0; i < 6; i++) {
                assert(q->regs[offsets[i] / 4] == values[i]);
                assert((values[i] & 0xffff) <= (values[i] >> 16));
                assert((values[i] >> 16) < 16384);
                before[offsets[i] / 4] = values[i];
            }
            assert(q->regs[REG_TXQ_CNGST_CFG / 4] ==
                   ((seeds[s] & ~owned) | 0x200700fa));
            assert((q->regs[REG_TXQ_CNGST_CFG / 4] &
                   (TXQ_CNGST_DROP_EN | TXQ_CNGST_DEI_DROP_EN)) ==
                   (seeds[s] & (TXQ_CNGST_DROP_EN | TXQ_CNGST_DEI_DROP_EN)));
            before[REG_TXQ_CNGST_CFG / 4] = (seeds[s] & ~owned) | 0x200700fa;
            assert(!memcmp(before, q->regs, sizeof(before)));
            q->count = 0;
            airoha_qdma_init_lan_sram_congestion(q);
            assert(q->count == 7);
            assert(!memcmp(before, q->regs, sizeof(before)));
        }
        struct airoha_qdma *other = &e.qdma[block ^ 1];
        assert(other->count == 0);
        for (unsigned r = 0; r < sizeof(other->regs) / sizeof(other->regs[0]); r++)
            assert(other->regs[r] == 0);
        cases++;
    }
    assert(cases == 480 && supported == 5);
    printf("PASS: %u real initializer cases; exact SDK bounds and scoped MMIO\n", cases);
}
'''
with tempfile.TemporaryDirectory(prefix='hg-congestion-profile-') as temp:
    path = Path(temp)
    (path / 'test.c').write_text(prelude + defines + '\n' + mmio + function + test)
    subprocess.run(['cc', '-std=gnu11', '-Wall', '-Wextra', '-Werror',
                    str(path / 'test.c'), '-o', str(path / 'test')], check=True)
    subprocess.run([str(path / 'test')], check=True)

init_start = source.index('static int airoha_qdma_hw_init(')
init_end = source.index('\nstatic ', init_start + 1)
init = source[init_start:init_end]
assert init.count('airoha_qdma_init_lan_sram_congestion(qdma);') == 1
assert init.index('airoha_qdma_init_lan_sram_congestion(qdma);') < init.index(
    'TXQ_CNGST_DROP_EN | TXQ_CNGST_DEI_DROP_EN')
assert source.count('airoha_qdma_init_lan_sram_congestion(qdma);') == 1
assert source.index('err = airoha_qdma_init_hfwd_queues(qdma);') < source.index(
    'return airoha_qdma_hw_init(qdma);')
print('PASS: initial pool setup precedes profile and congestion protection')
