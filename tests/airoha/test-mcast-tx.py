#!/usr/bin/env python3
"""Host-check the actual TX publication block with mock MMIO and batching.

No hardware is accessed; physical egress ordering remains a hardware test.
"""
import pathlib
import subprocess
import sys
import tempfile

source = pathlib.Path(sys.argv[1])
eth = (source / 'drivers/net/ethernet/airoha/airoha_eth.c').read_text()
tx = eth.split('static netdev_tx_t airoha_dev_xmit_common(', 1)[1].split('\nstatic ', 1)[0]
block = 'if (mc_sample ||' + tx.split('if (mc_sample ||', 1)[1].split('\n\tspin_unlock_bh(&q->lock);', 1)[0]
assert 'mc_sample = !ctrl && !pon_data && airoha_mcast_is_sample(skb);' in tx
assert tx.index('goto error_unmap') < tx.index('airoha_mcast_tx(dev, skb, msg0)')
assert tx.index('netdev_tx_sent_queue') < tx.index('airoha_mcast_tx(dev, skb, msg0)')
assert tx.count('airoha_mcast_tx(dev, skb, msg0)') == 1
prelude = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdio.h>
#include <string.h>
static char events[8];
static int count;
static bool more, stopped;
#define REG_TX_CPU_IDX(qid) (qid)
#define TX_RING_CPU_IDX_MASK 65535
#define FIELD_PREP(mask, index) (index)
#define netif_xmit_stopped(txq) (stopped)
#define netdev_xmit_more() (more)
#define dma_wmb() (events[count++] = 'D')
#define airoha_qdma_rmw(qdma, reg, mask, val) (events[count++] = 'W')
#define airoha_qdma_rr(qdma, reg) (events[count++] = 'R')
#define airoha_mcast_tx(dev, skb, msg0) (events[count++] = 'B')
static void submit(bool mc_sample) {
'''
main = r'''
}
int main(void) {
    for (int sample = 0; sample < 2; sample++)
        for (int batch = 0; batch < 2; batch++)
            for (int stop = 0; stop < 2; stop++) {
                count = 0;
                memset(events, 0, sizeof(events));
                more = batch;
                stopped = stop;
                submit(sample);
                assert(!strcmp(events, sample ? "DWRB" : (!batch || stop) ? "W" : ""));
            }
    puts("PASS: barrier/doorbell/readback precede BIND in all eight TX batching states");
}
'''
with tempfile.TemporaryDirectory(prefix='hg-mcast-tx-') as d:
    c = pathlib.Path(d) / 'test.c'
    binary = pathlib.Path(d) / 'test'
    c.write_text(prelude + block + main)
    subprocess.run(['cc', '-std=gnu99', '-Wall', '-Wextra', '-Werror',
                    str(c), '-o', str(binary)], check=True)
    subprocess.run([str(binary)], check=True)
