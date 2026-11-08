#!/usr/bin/env python3
"""Exercise the actual QDMA IRQ diagnostic helper without a device.

Checks cause filtering and per-bank accounting. It does not emulate the IRQ
controller, DMA coherency, PSE buffers, or reproduce the HG outage.
"""
import argparse
from pathlib import Path
import re
import subprocess
import tempfile

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('source', type=Path)
args = parser.parse_args()
root = args.source / 'drivers/net/ethernet/airoha'
source = (root / 'airoha_eth.c').read_text()
header = (root / 'airoha_regs.h').read_text()
start = source.index('static __always_inline void\nairoha_qdma_record_irq_error(')
end = source.index('{', start)
depth = 1
end += 1
while depth:
    depth += (source[end] == '{') - (source[end] == '}')
    end += 1
helper = source[start:end]
logical_header = header.replace('\\\n', '')
macros = '\n'.join(re.findall(r'^#define[^\n]*', logical_header, re.M)) + '\n'
irq = source[source.index('static irqreturn_t airoha_irq_handler('):]
irq = irq[:irq.index('\nstatic int airoha_qdma_init_irq_banks(')]
assert irq.index('intr[i] &= irq_bank->irqmask[i];') < irq.index('airoha_qdma_record_irq_error(')
assert irq.index('airoha_qdma_record_irq_error(') < irq.index('airoha_qdma_wr(qdma, REG_INT_STATUS(i), intr[i]);')
assert not re.search(r'airoha_(?:fe|qdma)_(?:rr|wr|rmw|set|clear)\(', helper)
assert '"qdma_irq_errors", 0400,' in source

prelude = r'''
#include <assert.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
typedef uint32_t u32;
typedef uint64_t u64;
#define BIT(n) (UINT32_C(1) << (n))
#define GENMASK(h, l) ((UINT32_MAX << (l)) & (UINT32_MAX >> (31-(h))))
#define IS_ENABLED(option) (option)
#define likely(x) (x)
#define READ_ONCE(x) (x)
#define WRITE_ONCE(x, value) ((x) = (value))
enum { QDMA_INT_REG_IDX0, QDMA_INT_REG_IDX1, QDMA_INT_REG_IDX2,
       QDMA_INT_REG_IDX3, QDMA_INT_REG_IDX4, QDMA_INT_REG_MAX };
struct airoha_irq_bank {
 u64 error_irqs[QDMA_INT_REG_MAX];
 u32 error_seen[QDMA_INT_REG_MAX], error_last[QDMA_INT_REG_MAX];
 unsigned long error_jiffies[QDMA_INT_REG_MAX];
};
static unsigned long jiffies;
'''
main = r'''
int main(void) {
 struct airoha_irq_bank bank = {0}, other = {0}, zero = {0};
 /* Normal RX/TX completions, including TX completion-queue FULL, are not
  * reclassified as coherent/no-descriptor error events by this helper. */
 airoha_qdma_record_irq_error(&bank, 0, INT_TX_MASK);
 airoha_qdma_record_irq_error(&bank, 1, RX_DONE_LOW_INT_MASK);
 airoha_qdma_record_irq_error(&bank, 2, RX_DONE_HIGH_INT_MASK);
 assert(!memcmp(&bank, &zero, sizeof(bank)));
 jiffies = 123;
 airoha_qdma_record_irq_error(&bank, 0, RX0_COHERENT_INT_MASK | TX7_COHERENT_INT_MASK | IRQ0_INT_MASK);
 jiffies = 124;
 airoha_qdma_record_irq_error(&bank, 0, RX6_COHERENT_INT_MASK);
 airoha_qdma_record_irq_error(&bank, 1, RX0_NO_CPU_DSCP_INT_MASK | RX0_DONE_INT_MASK);
 airoha_qdma_record_irq_error(&bank, 2, RX16_NO_CPU_DSCP_INT_MASK | RX16_DONE_INT_MASK);
 airoha_qdma_record_irq_error(&bank, 3, RX31_COHERENT_INT_MASK);
 airoha_qdma_record_irq_error(&bank, 4, TX8_COHERENT_INT_MASK);
 if (CONFIG_DEBUG_FS) {
  assert(bank.error_irqs[0] == 2);
  assert(bank.error_seen[0] == (RX0_COHERENT_INT_MASK | RX6_COHERENT_INT_MASK | TX7_COHERENT_INT_MASK));
  assert(bank.error_last[0] == RX6_COHERENT_INT_MASK && bank.error_jiffies[0] == 124);
  for (int i=1; i<5; i++) assert(bank.error_irqs[i] == 1 && bank.error_jiffies[i] == 124);
  assert(bank.error_last[1] == RX0_NO_CPU_DSCP_INT_MASK);
  assert(bank.error_last[2] == RX16_NO_CPU_DSCP_INT_MASK);
  assert(bank.error_last[3] == RX31_COHERENT_INT_MASK);
  assert(bank.error_last[4] == TX8_COHERENT_INT_MASK);
  airoha_qdma_record_irq_error(&other, 0, HWFWD_DSCP_EMPTY_INT_MASK | HWFWD_DSCP_LOW_INT_MASK);
  assert(other.error_irqs[0] == 1 && bank.error_irqs[0] == 2);
  assert(other.error_last[0] == (HWFWD_DSCP_EMPTY_INT_MASK | HWFWD_DSCP_LOW_INT_MASK));
 } else {
  assert(!memcmp(&bank, &zero, sizeof(bank)));
  assert(!memcmp(&other, &zero, sizeof(other)));
 }
 puts("PASS: real IRQ diagnostic helper; CONFIG_DEBUG_FS=" CONFIG_TEXT);
 return 0;
}
'''

with tempfile.TemporaryDirectory(prefix='hg-irq-errors-') as tmp:
    tmp = Path(tmp)
    program = tmp / 'test.c'
    program.write_text(prelude + '\n' + macros + '\n' + helper + '\n' + main)
    for enabled in (1, 0):
        exe = tmp / f'irq-errors-{enabled}'
        subprocess.run(['cc', '-std=gnu11', '-O2', '-Wall', '-Wextra', '-Werror',
                        f'-DCONFIG_DEBUG_FS={enabled}', f'-DCONFIG_TEXT="{enabled}"',
                        str(program), '-o', str(exe)], check=True)
        subprocess.run([str(exe)], check=True)
