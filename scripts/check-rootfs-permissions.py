#!/usr/bin/env python3
"""Reject host permission leakage which prevents OpenWrt's ubus startup.

Inspect the Unix modes that are written to the image, independently of the
builder's UID and host ACLs. Never repair modes: private files must stay private.
"""

import argparse
import os
from pathlib import Path, PurePosixPath
import stat
import sys


def resolve(root, name, traversed=None):
    pending = list(PurePosixPath(name).parts)
    parts = []
    links = 0
    while pending:
        part = pending.pop(0)
        if part in ("/", ".", ""):
            continue
        if part == "..":
            if not parts:
                raise ValueError(f"path escapes image: {name}")
            parts.pop()
            continue
        if traversed is not None:
            traversed.append(root.joinpath(*parts))
        path = root.joinpath(*parts, part)
        if path.is_symlink():
            links += 1
            if links > 40:
                raise ValueError(f"symlink loop: {name}")
            target = os.readlink(path)
            if target.startswith("/"):
                parts = []
            pending = list(PurePosixPath(target).parts) + pending
        else:
            parts.append(part)
    return root.joinpath(*parts)


def check(root):
    errors = []
    # These directories contain global programs/configuration, not private
    # runtime state. Inherited setgid/sticky bits are build contamination here.
    for name in ("/", "/bin", "/sbin", "/etc", "/lib", "/usr"):
        try:
            path = resolve(root, name)
            mode = path.stat().st_mode
            if not stat.S_ISDIR(mode) or mode & 0o7000:
                errors.append(f"{name}: unexpected directory mode {mode & 0o7777:04o}")
        except (OSError, ValueError) as exc:
            errors.append(f"{name}: {exc}")

    passwd = resolve(root, "/etc/passwd")
    try:
        accounts = [line.split(":") for line in passwd.read_text().splitlines()]
        account = next((a for a in accounts if a[0] == "ubus"), None)
        if account is None:
            # procd uses root when the image intentionally has no ubus user.
            return errors
        uid, gid = int(account[2]), int(account[3])
        groups = {gid}
        for row in resolve(root, "/etc/group").read_text().splitlines():
            fields = row.split(":")
            if len(fields) >= 4 and "ubus" in fields[3].split(","):
                groups.add(int(fields[2]))
    except (OSError, ValueError, IndexError) as exc:
        return errors + [f"cannot read ubus identity: {exc}"]

    def accessible(name, required):
        try:
            traversed = []
            path = resolve(root, name, traversed)
            checks = [(parent, 1) for parent in dict.fromkeys(traversed)]
            checks.append((path, required))
            for target, need in checks:
                info = target.stat()
                # Firmware packaging runs with --root-owned; files copied from
                # a non-root build owner must not inherit that host identity.
                owner, group = 0, 0
                shift = 6 if uid == owner else 3 if group in groups else 0
                allowed = (info.st_mode >> shift) & 7
                if uid == 0:
                    allowed = 7 if info.st_mode & 0o111 else 6
                if allowed & need != need:
                    relative = "/" + str(target.relative_to(root))
                    errors.append(f"ubus({uid}:{gid}) cannot access {name}: "
                                  f"{relative} mode {info.st_mode & 0o7777:04o}")
                    return
        except (OSError, ValueError) as exc:
            errors.append(f"{name}: {exc}")

    for name, required in (("/sbin/ubusd", 5), ("/etc/passwd", 4),
                           ("/etc/group", 4)):
        accessible(name, required)
    # Core shared libraries and loader used by ubusd on OpenWrt targets.
    for path in root.glob("lib/ld*.so*"):
        accessible("/" + str(path.relative_to(root)), 5)
    for pattern in ("lib/libc.so*", "lib/libubox.so*",
                    "lib/libblobmsg_json.so*", "lib/libjson-c.so*",
                    "usr/lib/libubox.so*", "usr/lib/libblobmsg_json.so*",
                    "usr/lib/libjson-c.so*"):
        for path in root.glob(pattern):
            accessible("/" + str(path.relative_to(root)), 4)
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("rootfs", type=Path)
    args = parser.parse_args()
    errors = check(args.rootfs.absolute())
    if errors:
        print("Rootfs startup permission check failed:", file=sys.stderr)
        for error in errors:
            print("  " + error, file=sys.stderr)
        print("Rebuild in a directory without inherited default ACLs; "
              "do not chmod private target files indiscriminately.", file=sys.stderr)
        return 1
    print("Rootfs startup permission check passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
