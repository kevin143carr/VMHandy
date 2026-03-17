from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


CHUNK_SIZE = 8 * 1024 * 1024


class CopyCancelledError(RuntimeError):
    pass


@dataclass(slots=True)
class VmSelection:
    source_vm: Path
    local_parent: Path

    @property
    def local_vm(self) -> Path:
        return self.local_parent / self.source_vm.name


def ensure_pvm(path: Path) -> None:
    if not path.exists():
        raise FileNotFoundError(f"VM path does not exist: {path}")
    if path.suffix.lower() != ".pvm" or not path.is_dir():
        raise ValueError(f"Expected a Parallels VM bundle (.pvm directory): {path}")


def list_vm_bundles(folder: Path) -> list[Path]:
    if not folder.exists():
        raise FileNotFoundError(f"Folder does not exist: {folder}")
    if not folder.is_dir():
        raise NotADirectoryError(f"Expected a folder: {folder}")
    return sorted(
        [item for item in folder.iterdir() if item.is_dir() and item.suffix.lower() == ".pvm"],
        key=lambda item: item.name.lower(),
    )


def compute_total_size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def resolve_target_folder(path: Path) -> Path:
    return path if path.exists() else path.parent


def available_bytes(path: Path) -> int:
    target = resolve_target_folder(path)
    usage = shutil.disk_usage(target)
    return usage.free


def can_write_to_folder(path: Path) -> bool:
    target = resolve_target_folder(path)
    return target.exists() and os.access(target, os.W_OK)


def _backup_path_for(path: Path) -> Path:
    candidate = path.with_name(f"{path.name}.vmhandy-backup")
    counter = 1
    while candidate.exists():
        candidate = path.with_name(f"{path.name}.vmhandy-backup-{counter}")
        counter += 1
    return candidate


def _mark_as_bundle_macos(path: Path) -> None:
    attr_name = "com.apple.FinderInfo"
    read_result = subprocess.run(
        ["xattr", "-px", attr_name, str(path)],
        check=False,
        capture_output=True,
        text=True,
    )
    if read_result.returncode == 0:
        hex_str = "".join(read_result.stdout.split())
    else:
        hex_str = "0" * 64

    data = bytearray.fromhex(hex_str.ljust(64, "0"))
    data[8] |= 0x20

    write_result = subprocess.run(
        ["xattr", "-wx", attr_name, data.hex(), str(path)],
        check=False,
        capture_output=True,
        text=True,
    )
    if write_result.returncode != 0:
        message = write_result.stderr.strip() or write_result.stdout.strip() or "xattr -wx failed"
        raise OSError(f"Unable to mark VM bundle as a macOS bundle: {message}")

    touch_result = subprocess.run(
        ["touch", str(path)],
        check=False,
        capture_output=True,
        text=True,
    )
    if touch_result.returncode != 0:
        message = touch_result.stderr.strip() or touch_result.stdout.strip() or "touch failed"
        raise OSError(f"Unable to refresh the VM bundle in Finder: {message}")


def copy_tree_with_progress(
    source: Path,
    destination: Path,
    on_progress,
    overwrite: bool = False,
    should_cancel=lambda: False,
) -> None:
    ensure_pvm(source)
    if destination.exists() and not overwrite:
        raise FileExistsError(f"Destination already exists: {destination}")

    backup_path: Path | None = None
    if destination.exists() and overwrite:
        backup_path = _backup_path_for(destination)
        destination.rename(backup_path)

    total_bytes = compute_total_size(source)
    copied_bytes = 0

    try:
        for directory in [source, *[p for p in source.rglob("*") if p.is_dir()]]:
            if should_cancel():
                raise CopyCancelledError("Copy cancelled.")
            relative_dir = directory.relative_to(source)
            (destination / relative_dir).mkdir(parents=True, exist_ok=True)

        for item in source.rglob("*"):
            if should_cancel():
                raise CopyCancelledError("Copy cancelled.")
            relative_path = item.relative_to(source)
            destination_path = destination / relative_path
            if item.is_dir():
                destination_path.mkdir(parents=True, exist_ok=True)
                continue

            with item.open("rb") as src_handle, destination_path.open("wb") as dst_handle:
                while chunk := src_handle.read(CHUNK_SIZE):
                    if should_cancel():
                        raise CopyCancelledError("Copy cancelled.")
                    dst_handle.write(chunk)
                    copied_bytes += len(chunk)
                    on_progress(copied_bytes, total_bytes, str(relative_path))
            shutil.copystat(item, destination_path, follow_symlinks=False)
        for directory in [*source.rglob("*"), source]:
            if not directory.is_dir():
                continue
            destination_path = destination / directory.relative_to(source) if directory != source else destination
            shutil.copystat(directory, destination_path, follow_symlinks=False)
        if sys.platform == "darwin":
            _mark_as_bundle_macos(destination)
    except Exception:
        if destination.exists():
            remove_tree(destination)
        if backup_path is not None and backup_path.exists():
            backup_path.rename(destination)
        raise

    if backup_path is not None and backup_path.exists():
        remove_tree(backup_path)


def remove_tree(path: Path) -> None:
    if not path.exists():
        return
    shutil.rmtree(path)
