#!/usr/bin/env python3
"""Compile actual sysfs show functions with host stubs; no ASIC access."""
import argparse
from pathlib import Path
import re
import subprocess
import tempfile

p = argparse.ArgumentParser()
p.add_argument('linux_tree', type=Path)
a = p.parse_args()
src = (a.linux_tree / 'drivers/net/ethernet/airoha/airoha_npu.c').read_text()
functions = []
for name in ('fw_version', 'num_cores'):
    match = re.search(r'static ssize_t ' + name + r'_show\(.*?\n}\n', src, re.S)
    assert match, name
    body = match[0]
    assert not re.search(r'regmap_|mbox|msg_get|readl|writel', body)
    assert f'DEVICE_ATTR_RO({name})' in src
    functions.append(body)
assert '.dev_groups = airoha_npu_groups' in src
assert src.count('WLAN_FUNC_GET_WAIT_NPU_VERSION,') == 1
assert 'npu->fw_version = val;' in src and 'npu->fw_version_valid = true;' in src
assert src.index('platform_set_drvdata(pdev, npu);') < src.index('.dev_groups = airoha_npu_groups')
header = r'''
#include <assert.h>
#include <errno.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <sys/types.h>
#define ARRAY_SIZE(a) (sizeof(a) / sizeof((a)[0]))
#define sysfs_emit(buf, ...) snprintf(buf, 4096, __VA_ARGS__)
struct airoha_npu { uint32_t fw_version; bool fw_version_valid; int cores[TEST_CORES]; };
struct device { struct airoha_npu *npu; };
struct device_attribute { int unused; };
static void *dev_get_drvdata(struct device *dev) { return dev->npu; }
'''
main = r'''
int main(void) {
    struct airoha_npu npu = {0};
    struct device dev = { .npu = &npu };
    char buf[4096], expected[64];
    assert(fw_version_show(&dev, NULL, buf) == -ENODATA);
    npu.fw_version_valid = true;
    assert(fw_version_show(&dev, NULL, buf) == 4 && !strcmp(buf, "0.0\n"));
    npu.fw_version = (1456u << 16) | 62;
    assert(fw_version_show(&dev, NULL, buf) == 8 && !strcmp(buf, "1456.62\n"));
    npu.fw_version = UINT32_MAX;
    assert(fw_version_show(&dev, NULL, buf) == 12 && !strcmp(buf, "65535.65535\n"));
    snprintf(expected, sizeof(expected), "%d\n", TEST_CORES);
    assert(num_cores_show(&dev, NULL, buf) > 0 && !strcmp(buf, expected));
    return 0;
}
'''
# 1456.62\n is eight bytes; max version is twelve incl. newline.
with tempfile.TemporaryDirectory(prefix='hg-npu-sysfs-') as d:
    source = Path(d) / 'show.c'
    source.write_text(header + '\n'.join(functions) + main)
    for cores in (4, 8):
        output = Path(d) / f'show-{cores}'
        subprocess.run(['cc', '-std=gnu11', '-Wall', '-Wextra', '-Werror',
                        '-Wno-unused-parameter', f'-DTEST_CORES={cores}',
                        str(source), '-o', str(output)], check=True)
        subprocess.run([str(output)], check=True)
print('PASS: actual sysfs show functions, ENODATA/zero/max version, variable core arrays; read-only/cache-only wiring')
