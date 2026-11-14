#!/usr/bin/env python3
"""Local regressions for build-host ACL leakage into rootfs Unix modes."""

import importlib.util
import os
from pathlib import Path
import tempfile
import unittest

script = Path(__file__).resolve().parents[2] / "scripts/check-rootfs-permissions.py"
spec = importlib.util.spec_from_file_location("permissions", script)
permissions = importlib.util.module_from_spec(spec)
spec.loader.exec_module(permissions)


class RootfsPermissions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.root.chmod(0o755)
        for name in ("bin", "sbin", "etc", "lib", "usr"):
            (self.root / name).mkdir()
            (self.root / name).chmod(0o755)
        for name, data, mode in (
            ("etc/passwd", "ubus:x:81:81:ubus:/var/run/ubus:/bin/false\n", 0o644),
            ("etc/group", "root:x:0:\nubus:x:81:ubus\n", 0o644),
            ("sbin/ubusd", "placeholder\n", 0o755),
            ("lib/libc.so", "placeholder\n", 0o755),
            ("etc/shadow", "private\n", 0o600),
        ):
            (self.root / name).write_text(data)
            (self.root / name).chmod(mode)

    def test_normal_image_and_private_shadow(self):
        self.assertEqual(permissions.check(self.root), [])
        self.assertEqual((self.root / "etc/shadow").stat().st_mode & 0o777, 0o600)

    def test_denied_directory_search(self):
        (self.root / "sbin").chmod(0o750)
        self.assertTrue(any("cannot access /sbin/ubusd" in e for e in permissions.check(self.root)))

    def test_denied_binary_execute(self):
        (self.root / "sbin/ubusd").chmod(0o750)
        self.assertTrue(permissions.check(self.root))

    def test_denied_shared_library_read(self):
        (self.root / "lib/libc.so").chmod(0o750)
        self.assertTrue(any("libc.so" in e for e in permissions.check(self.root)))

    def test_loader_needs_execute_as_well_as_read(self):
        (self.root / "lib/ld-musl-aarch64.so.1").symlink_to("/lib/libc.so")
        (self.root / "lib/libc.so").chmod(0o644)
        self.assertTrue(any("ld-musl" in e for e in permissions.check(self.root)))

    def test_denied_account_database(self):
        (self.root / "etc/passwd").chmod(0o640)
        self.assertTrue(any("/etc/passwd" in e for e in permissions.check(self.root)))

    def test_inherited_special_directory_bits(self):
        (self.root / "etc").chmod(0o2775)
        self.assertTrue(any("unexpected directory mode" in e for e in permissions.check(self.root)))

    def test_absolute_symlinks_resolve_inside_image(self):
        (self.root / "lib/libc.so").rename(self.root / "lib/actual-libc.so")
        (self.root / "lib/libc.so").symlink_to("/lib/actual-libc.so")
        self.assertEqual(permissions.check(self.root), [])
        (self.root / "lib/actual-libc.so").chmod(0o600)
        self.assertTrue(permissions.check(self.root))

    def test_symlink_cannot_hide_inaccessible_parent(self):
        (self.root / "sbin/ubusd").unlink()
        (self.root / "sbin/ubusd").symlink_to("/lib/libc.so")
        (self.root / "lib").chmod(0o750)
        self.assertTrue(permissions.check(self.root))

    def test_symlink_parent_itself_requires_search(self):
        (self.root / "sbin/ubusd").unlink()
        (self.root / "sbin/ubusd").symlink_to("/lib/libc.so")
        (self.root / "sbin").chmod(0o750)
        self.assertTrue(permissions.check(self.root))


if __name__ == "__main__":
    unittest.main()
