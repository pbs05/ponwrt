#!/usr/bin/env python3
"""Check queue/bank associations and map lifecycle in the prepared driver."""
import pathlib
import subprocess
import sys
import tempfile

root = pathlib.Path(sys.argv[1]) / 'drivers/net/ethernet/airoha'
source = (root / 'airoha_eth.c').read_text()
header = (root / 'airoha_eth.h').read_text()
macros = header.split('#define RX_IRQ0_BANK_PIN_MASK', 1)[1].split('\nstruct ', 1)[0]
helper = source.split('static int airoha_rx_irq_bank(', 1)[1].split('\nstatic int ', 1)[0]
program = '#include <assert.h>\n#define BIT(n) (1U << (n))\n#define AIROHA_MAX_NUM_IRQ_BANKS 4\n'
program += '#define RX_IRQ0_BANK_PIN_MASK' + macros + '\nstatic int airoha_rx_irq_bank(' + helper
program += '''
int main(void) {
    for (int q = 0; q < 32; q++) {
        int n = 0, bank = -1;
        for (int b = 0; b < 4; b++)
            if (RX_IRQ_BANK_PIN_MASK(b) & BIT(q)) { n++; bank = b; }
        assert(n <= 1);
        assert(airoha_rx_irq_bank(q) == bank);
    }
    assert(airoha_rx_irq_bank(0) == 0);
    assert(airoha_rx_irq_bank(6) == 3);
    return 0;
}
'''
with tempfile.TemporaryDirectory(prefix='hg-napi-map-') as d:
    p = pathlib.Path(d); (p / 'test.c').write_text(program)
    subprocess.run(['cc', '-std=c11', '-Wall', '-Wextra', '-Werror', str(p / 'test.c'), '-o', str(p / 'test')], check=True)
    subprocess.run([str(p / 'test')], check=True)
remove = source.split('static void airoha_remove(', 1)[1]
assert remove.index('debugfs_remove_recursive') < remove.index('airoha_qdma_stop_napi')
show = source.split('static int airoha_napi_map_show(', 1)[1].split('DEFINE_SHOW_ATTRIBUTE', 1)[0]
assert show.index('netdev_lock') < show.index('task_pid_nr') < show.index('netdev_unlock')
assert 'netif_napi_set_irq(&irq_q->napi, qdma->irq_banks[0].irq)' in source
print('PASS: 32 bank mappings, TX association and reader teardown ordering')
