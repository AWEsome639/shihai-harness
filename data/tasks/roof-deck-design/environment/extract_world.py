#!/usr/bin/env python3
from __future__ import annotations

import shutil
import stat
import sys
import zipfile
from pathlib import Path, PurePosixPath


def main() -> None:
    archive_path = Path(sys.argv[1]).resolve(strict=True)
    destination = Path(sys.argv[2]).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    total = 0
    with zipfile.ZipFile(archive_path) as archive:
        for info in archive.infolist():
            raw_name = info.filename.replace("\\", "/")
            member = PurePosixPath(raw_name)
            mode = info.external_attr >> 16
            if member.is_absolute() or not member.parts or ".." in member.parts:
                raise ValueError(f"unsafe archive member: {raw_name!r}")
            if stat.S_ISLNK(mode):
                raise ValueError(f"symlink archive member: {raw_name!r}")
            total += info.file_size
            if total > 4 * 1024**3:
                raise ValueError("archive expands beyond 4 GiB")
            if member.parts[0] == "__MACOSX":
                continue
            if member.parts[0] != "filesystem":
                raise ValueError(f"unexpected archive root: {raw_name!r}")
            relative_parts = member.parts[1:]
            if not relative_parts:
                if info.is_dir():
                    continue
                raise ValueError(f"archive root must be a directory: {raw_name!r}")
            target = destination.joinpath(*relative_parts)
            resolved_parent = target.parent.resolve()
            if destination != resolved_parent and destination not in resolved_parent.parents:
                raise ValueError(f"archive member escapes destination: {raw_name!r}")
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, target.open("wb") as output:
                shutil.copyfileobj(source, output, length=1024 * 1024)


if __name__ == "__main__":
    main()
