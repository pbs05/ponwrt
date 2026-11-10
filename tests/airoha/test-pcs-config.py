#!/usr/bin/env python3
"""Compile the real PCS pre/post callbacks against fault-injected MMIO."""
from pathlib import Path
import re
import subprocess
import sys
import tempfile

base = Path(sys.argv[1]) / 'drivers/net/pcs/airoha'
source = (base / 'pcs-airoha-common.c').read_text()
header = (base / 'pcs-airoha.h').read_text()


def function(name):
    start = re.search(r'^static [^\n]*\b' + name + r'\(', source, re.M).start()
    opening = source.index('{', start)
    depth = 1
    end = opening + 1
    while depth:
        depth += (source[end] == '{') - (source[end] == '}')
        end += 1
    return source[start:end]


functions = '\n'.join(function(n) for n in (
    'airoha_pcs_config_failed', 'airoha_pcs_pre_config',
    'airoha_pcs_post_config'))
names = set(re.findall(r'\bAIROHA_[A-Z0-9_]+\b', functions))
defines = '\n'.join(re.search(r'^#define\s+' + n + r'\s+.+$', header, re.M)[0]
                    for n in sorted(names))
prelude = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stddef.h>
#include <stdio.h>
#include <string.h>
#define BIT(n) (1U << (n))
#define GENMASK(h,l) ((~0U >> (31-(h))) & (~0U << (l)))
#define FIELD_PREP(m,v) (((unsigned)(v) << __builtin_ctz(m)) & (m))
#define dev_err_ratelimited(...) ((void)0)
#define usleep_range(...) ((void)0)
#define guard(type) (void)
typedef int phy_interface_t;
struct regmap { uint32_t r[128]; };
struct phylink_pcs { int unused; };
struct airoha_pcs_maps { struct regmap *pcs_mac; };
struct airoha_pcs_priv { struct airoha_pcs_maps maps[1]; void *dev; };
struct airoha_pcs_port {
    struct airoha_pcs_priv *priv; int index, config_error, lock;
    struct phylink_pcs pcs;
};
#define to_airoha_pcs_port(p) ((struct airoha_pcs_port *)((char *)(p) - offsetof(struct airoha_pcs_port,pcs)))
static void __attribute__((unused)) airoha_pcs_recovery_stop(struct airoha_pcs_port *p, bool disable) {
    (void)p; (void)disable;
}
static int op, fail, writes;
static int airoha_pcs_setup_scu(struct airoha_pcs_priv *p, int i, int mode) {
    (void)p; (void)i; (void)mode;
    return ++op == fail ? -5 : 0;
}
static int regmap_update_bits(struct regmap *m, unsigned r, unsigned mask, unsigned v) {
    if (++op == fail) return -5;
    assert(r / 4 < 128); writes++;
    m->r[r/4] = (m->r[r/4] & ~mask) | (v & mask);
    return 0;
}
static int regmap_set_bits(struct regmap *m, unsigned r, unsigned mask) {
    return regmap_update_bits(m, r, mask, mask);
}
static int regmap_clear_bits(struct regmap *m, unsigned r, unsigned mask) {
    return regmap_update_bits(m, r, mask, 0);
}
'''
checks = r'''
int main(void) {
    unsigned cases = 0;
    for (int inject = 0; inject <= 8; inject++) {
        struct regmap mac = {0};
        struct airoha_pcs_priv priv = {.maps={{.pcs_mac=&mac}}};
        struct airoha_pcs_port p = {.priv=&priv};
        const unsigned stop = 15;
        op=0; fail=inject; writes=0;
        airoha_pcs_pre_config(&p.pcs, 0);
        int result = airoha_pcs_post_config(&p.pcs, 0);
        assert(result == (inject ? -5 : 0));
        assert(p.config_error == result);
        assert((mac.r[0] & stop) == stop);
        if (inject) {
            assert(op == inject + 1); /* only best-effort stop after failure */
            int previous = op;
            assert(airoha_pcs_post_config(&p.pcs, 0) == -5);
            assert(op == previous);
            assert(airoha_pcs_config_failed(&p, -12) == -5);
        }
        op=fail=0;
        airoha_pcs_pre_config(&p.pcs, 0);
        assert(airoha_pcs_post_config(&p.pcs, 0) == 0);
        assert(p.config_error == 0);
        assert(((mac.r[0] >> 22) & 31) == 31);
        assert(((mac.r[0] >> 17) & 31) == 31);
        assert(((mac.r[0] >> 10) & 63) == 10);
        cases++;
    }
    printf("PCS configuration: %u failure/retry cases passed\n", cases);
}
'''
# The void pre callback cannot propagate an error itself; both later callbacks
# must reject it before bringup / link-up can write to the hardware.
for name in ('airoha_pcs_config', 'airoha_pcs_link_up'):
    body = function(name)
    assert body.index('if (port->config_error)') < body.index('maps =')
with tempfile.TemporaryDirectory(prefix='hg-pcs-config-') as d:
    c = Path(d) / 'test.c'
    exe = Path(d) / 'test'
    c.write_text(prelude + defines + '\n' + functions + '\n' + checks)
    subprocess.run(['cc', '-std=gnu11', '-Wall', '-Wextra', '-Werror',
                    '-Wno-unused-parameter', str(c), '-o', str(exe)], check=True)
    subprocess.run([str(exe)], check=True)
