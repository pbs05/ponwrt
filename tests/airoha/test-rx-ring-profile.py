#!/usr/bin/env python3
"""Execute the real descriptor selector in a host harness (prepared kernel)."""
import pathlib
import subprocess
import sys
import tempfile

root = pathlib.Path(sys.argv[1])
source = (root / 'drivers/net/ethernet/airoha/airoha_eth.c').read_text()
header = (root / 'drivers/net/ethernet/airoha/airoha_eth.h').read_text()
selector = source.split('static int airoha_qdma_rx_desc_count(', 1)[1].split('\nstatic int ', 1)[0]
macro = header.split('#define RX_DSCP_NUM', 1)[1].split('\n\n', 1)[0]
test = '''
#include <assert.h>
#include <stdbool.h>
static bool multicast;
#define IS_ENABLED(x) multicast
struct airoha_eth;
struct airoha_qdma { struct airoha_eth *eth; };
struct airoha_eth { struct airoha_qdma qdma[2]; bool an7581; };
static bool airoha_is_7581(struct airoha_eth *e) { return e->an7581; }
'''
test += '#define RX_DSCP_NUM' + macro + '\nstatic int airoha_qdma_rx_desc_count(' + selector
test += '''
int main(void) {
    struct airoha_eth eth = {0};
    for (int chip = 0; chip < 2; chip++)
    for (int feature = 0; feature < 2; feature++)
    for (int d = 0; d < 2; d++)
    for (int r = 0; r < 32; r++) {
        eth.an7581 = chip; multicast = feature; eth.qdma[d].eth = &eth;
        int expected = chip && feature && d == 1 && r == 6 ? 1024 : RX_DSCP_NUM(r);
        assert(airoha_qdma_rx_desc_count(&eth.qdma[d], r) == expected);
    }
    return 0;
}
'''
with tempfile.TemporaryDirectory(prefix='hg-rx-profile-') as d:
    path = pathlib.Path(d); (path / 'test.c').write_text(test)
    subprocess.run(['cc', '-std=c11', '-Wall', '-Wextra', '-Werror', str(path / 'test.c'), '-o', str(path / 'test')], check=True)
    subprocess.run([str(path / 'test')], check=True)
print('PASS: 256 descriptor profile combinations')
