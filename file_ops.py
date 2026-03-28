from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


CHUNK_SIZE = 8 * 1024 * 1024
PROVIDER_PARALLELS = "parallels"
PROVIDER_VMWARE_FUSION = "vmware_fusion"
PROVIDER_LABELS = {
    PROVIDER_PARALLELS: "Parallels",
    PROVIDER_VMWARE_FUSION: "VMware Fusion",
}
BUNDLE_SUFFIXES = {
    PROVIDER_PARALLELS: ".pvm",
    PROVIDER_VMWARE_FUSION: ".vmwarevm",
}
VMWARE_FUSION_INVENTORY_PATH = Path("~/Library/Application Support/VMware Fusion/vmInventory").expanduser()
COMMON_MACOS_BIN_DIRS = (
    Path("/opt/homebrew/bin"),
    Path("/usr/local/bin"),
    Path("/usr/bin"),
    Path("/bin"),
)
PARALLELS_APP_NAME = "Parallels Desktop"
VMWARE_FUSION_APP_NAME = "VMware Fusion"
CONFIGURED_EXECUTABLES: dict[str, str] = {
    PROVIDER_PARALLELS: "",
    PROVIDER_VMWARE_FUSION: "",
}


class CopyCancelledError(RuntimeError):
    pass


@dataclass(slots=True)
class VmSelection:
    source_vm: Path
    local_parent: Path

    @property
    def local_vm(self) -> Path:
        return self.local_parent / self.source_vm.name


@dataclass(slots=True)
class RegisteredVm:
    id: str
    name: str
    path: Path
    status: str | None = None


class VmProvider:
    name = ""
    label = ""
    supports_registration = False
    supports_unregistration = False
    supports_launch = False

    def is_available(self) -> bool:
        raise NotImplementedError

    def list_registered_vms(self) -> list[RegisteredVm]:
        raise NotImplementedError

    def register_vm(self, vm_path: Path) -> None:
        raise NotImplementedError

    def unregister_vm(self, vm_id: str) -> None:
        raise NotImplementedError

    def launch_vm(self, vm: RegisteredVm) -> None:
        raise NotImplementedError

    def find_registered_vm(self, vm_path: Path) -> RegisteredVm | None:
        normalized_path = _normalize_path(vm_path)
        for vm in self.list_registered_vms():
            if _normalize_path(vm.path) == normalized_path:
                return vm
        return None


class ParallelsProvider(VmProvider):
    name = PROVIDER_PARALLELS
    label = PROVIDER_LABELS[PROVIDER_PARALLELS]
    supports_registration = True
    supports_unregistration = True
    supports_launch = True

    def is_available(self) -> bool:
        return _parallels_cli_path() is not None

    def list_registered_vms(self) -> list[RegisteredVm]:
        if not self.is_available():
            return []
        output = _run_cli(["prlctl", "list", "--all", "-i"])
        return _parse_parallels_registered_vms(output)

    def register_vm(self, vm_path: Path) -> None:
        ensure_pvm(vm_path)
        _run_cli(["prlctl", "register", str(vm_path), "--preserve-uuid"])

    def unregister_vm(self, vm_id: str) -> None:
        _run_cli(["prlctl", "unregister", vm_id])

    def launch_vm(self, vm: RegisteredVm) -> None:
        ensure_pvm(vm.path)
        _run_cli(["open", "-g", "-a", "Parallels Desktop", str(vm.path)])


class VmwareFusionProvider(VmProvider):
    name = PROVIDER_VMWARE_FUSION
    label = PROVIDER_LABELS[PROVIDER_VMWARE_FUSION]
    supports_launch = True

    def is_available(self) -> bool:
        return (
            _vmware_inventory_path().exists()
            or _vmware_cli_path() is not None
            or Path("/Applications/VMware Fusion.app/Contents/Public/vmrun").exists()
        )

    @property
    def supports_registration(self) -> bool:
        inventory_path = _vmware_inventory_path()
        return inventory_path.exists() and _is_writable_file(inventory_path)

    @property
    def supports_unregistration(self) -> bool:
        inventory_path = _vmware_inventory_path()
        return inventory_path.exists() and _is_writable_file(inventory_path)

    def list_registered_vms(self) -> list[RegisteredVm]:
        inventory_path = _vmware_inventory_path()
        if not self.is_available() or not inventory_path.exists():
            return []
        return _parse_vmware_inventory(inventory_path)

    def register_vm(self, vm_path: Path) -> None:
        ensure_vm_bundle(vm_path, self.name)
        inventory_path = _vmware_inventory_path()
        if not inventory_path.exists():
            raise RuntimeError(f"VMware Fusion inventory file does not exist: {inventory_path}")
        if not _is_writable_file(inventory_path):
            raise PermissionError(f"VMware Fusion inventory file is not writable: {inventory_path}")
        _register_vmware_inventory_vm(inventory_path, vm_path)

    def unregister_vm(self, vm_id: str) -> None:
        inventory_path = _vmware_inventory_path()
        if not inventory_path.exists():
            raise RuntimeError(f"VMware Fusion inventory file does not exist: {inventory_path}")
        if not _is_writable_file(inventory_path):
            raise PermissionError(f"VMware Fusion inventory file is not writable: {inventory_path}")
        _unregister_vmware_inventory_vm(inventory_path, vm_id)

    def launch_vm(self, vm: RegisteredVm) -> None:
        vmx_path = Path(vm.id).expanduser()
        if vmx_path.suffix.lower() != ".vmx" or not vmx_path.exists():
            vmx_path = _find_vmware_vmx(vm.path)
        _run_cli(["vmrun", "-T", "fusion", "start", str(vmx_path)])


def ensure_vm_bundle(path: Path, provider_name: str) -> None:
    if not path.exists():
        raise FileNotFoundError(f"VM path does not exist: {path}")
    expected_suffix = vm_bundle_suffix(provider_name)
    if path.suffix.lower() != expected_suffix or not path.is_dir():
        raise ValueError(
            f"Expected a {provider_label(provider_name)} VM bundle ({expected_suffix} directory): {path}"
        )


def ensure_pvm(path: Path) -> None:
    ensure_vm_bundle(path, PROVIDER_PARALLELS)


def vm_bundle_suffix(provider_name: str) -> str:
    return BUNDLE_SUFFIXES.get(provider_name, ".pvm")


def list_vm_bundles(folder: Path, provider_name: str = PROVIDER_PARALLELS) -> list[Path]:
    if not folder.exists():
        raise FileNotFoundError(f"Folder does not exist: {folder}")
    if not folder.is_dir():
        raise NotADirectoryError(f"Expected a folder: {folder}")
    suffix = vm_bundle_suffix(provider_name)
    return sorted(
        [item for item in folder.iterdir() if item.is_dir() and item.suffix.lower() == suffix],
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


def can_write_to_file(path: Path) -> bool:
    return path.exists() and _is_writable_file(path)


def available_provider_names() -> list[str]:
    return [PROVIDER_PARALLELS, PROVIDER_VMWARE_FUSION]


def available_providers() -> dict[str, VmProvider]:
    return {name: get_provider(name) for name in available_provider_names()}


def default_provider_name() -> str:
    providers = available_providers()
    available = [name for name, provider in providers.items() if provider.is_available()]
    if PROVIDER_PARALLELS in available and PROVIDER_VMWARE_FUSION not in available:
        return PROVIDER_PARALLELS
    if PROVIDER_VMWARE_FUSION in available and PROVIDER_PARALLELS not in available:
        return PROVIDER_VMWARE_FUSION
    return PROVIDER_PARALLELS


def provider_label(name: str) -> str:
    return PROVIDER_LABELS.get(name, name)


def get_provider(name: str) -> VmProvider:
    if name == PROVIDER_VMWARE_FUSION:
        return VmwareFusionProvider()
    return ParallelsProvider()


def set_configured_executable(provider_name: str, executable_path: str) -> None:
    if provider_name not in CONFIGURED_EXECUTABLES:
        return
    CONFIGURED_EXECUTABLES[provider_name] = executable_path.strip()


def configured_executable(provider_name: str) -> str:
    return CONFIGURED_EXECUTABLES.get(provider_name, "")


def parallels_running_vm_count() -> int:
    provider = ParallelsProvider()
    if not provider.is_available():
        return 0
    output = _run_cli(["prlctl", "list", "--no-header"])
    return len([line for line in output.splitlines() if line.strip()])


def vmware_running_vm_count() -> int:
    provider = VmwareFusionProvider()
    if not provider.is_available():
        return 0
    output = _run_cli(["vmrun", "-T", "fusion", "list"])
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if not lines:
        return 0
    return max(len(lines) - 1, 0)


def minimize_parallels_control_center() -> str:
    script = f"""
tell application "System Events"
    if not (exists process "{PARALLELS_APP_NAME}") then
        return "process-missing"
    end if
    tell process "{PARALLELS_APP_NAME}"
        repeat with targetWindow in windows
            try
                set windowTitle to name of targetWindow
            on error
                set windowTitle to ""
            end try
            if windowTitle contains "Control Center" then
                try
                    set value of attribute "AXMinimized" of targetWindow to true
                    return "minimized"
                on error errorMessage
                    return "error:" & errorMessage
                end try
            end if
        end repeat
    end tell
end tell
return "not-found"
"""
    try:
        return _run_osascript(script).strip() or "unknown"
    except Exception as exc:  # noqa: BLE001
        return f"error:{exc}"


def quit_parallels_desktop() -> None:
    _run_osascript(f'tell application "{PARALLELS_APP_NAME}" to quit')


def quit_vmware_fusion() -> None:
    _run_osascript(f'tell application "{VMWARE_FUSION_APP_NAME}" to quit')


def provider_unavailable_message(provider_name: str) -> str:
    if provider_name == PROVIDER_PARALLELS:
        search_locations = _executable_search_locations("prlctl", configured_executable(PROVIDER_PARALLELS))
        search_text = ", ".join(search_locations)
        return f"Parallels CLI 'prlctl' was not found. Checked: {search_text}. Configure it in the Parallels CLI path field if needed."
    if provider_name == PROVIDER_VMWARE_FUSION:
        return "VMware Fusion is not available on this Mac."
    return f"{provider_label(provider_name)} is not available on this Mac."


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
    provider_name: str = PROVIDER_PARALLELS,
) -> None:
    ensure_vm_bundle(source, provider_name)
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


def _normalize_path(path: Path) -> Path:
    return path.expanduser().resolve(strict=False)


def _run_cli(command: list[str]) -> str:
    resolved_command = _resolve_command(command)
    result = subprocess.run(resolved_command, check=False, capture_output=True, text=True)
    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or "Command failed"
        raise RuntimeError(message)
    return result.stdout


def _run_osascript(script: str) -> str:
    result = subprocess.run(["osascript", "-e", script], check=False, capture_output=True, text=True)
    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or "osascript failed"
        raise RuntimeError(message)
    return result.stdout


def _resolve_command(command: list[str]) -> list[str]:
    if not command:
        raise ValueError("Command cannot be empty.")
    executable_name = command[0]
    if executable_name == "prlctl":
        resolved = _parallels_cli_path()
    elif executable_name == "vmrun":
        resolved = _vmware_cli_path()
    else:
        resolved = _find_executable(executable_name)
    return [resolved or command[0], *command[1:]]


def _find_executable(name: str) -> str | None:
    return _find_executable_with_config(name)


def _find_executable_with_config(name: str, configured_path: str = "") -> str | None:
    configured = _validated_executable_path(configured_path)
    if configured is not None:
        return configured
    resolved = shutil.which(name)
    if resolved is not None:
        return resolved
    for directory in COMMON_MACOS_BIN_DIRS:
        candidate = directory / name
        if candidate.exists() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def _validated_executable_path(configured_path: str) -> str | None:
    if not configured_path.strip():
        return None
    candidate = Path(configured_path).expanduser()
    if candidate.exists() and candidate.is_file() and os.access(candidate, os.X_OK):
        return str(candidate)
    return None


def _executable_search_locations(name: str, configured_path: str = "") -> list[str]:
    locations: list[str] = []
    if configured_path.strip():
        locations.append(str(Path(configured_path).expanduser()))
    locations.append("PATH")
    locations.extend(str(directory / name) for directory in COMMON_MACOS_BIN_DIRS)
    deduped: list[str] = []
    seen: set[str] = set()
    for location in locations:
        if location in seen:
            continue
        deduped.append(location)
        seen.add(location)
    return deduped


def _parallels_cli_path() -> str | None:
    return _find_executable_with_config("prlctl", configured_executable(PROVIDER_PARALLELS))


def _vmware_cli_path() -> str | None:
    return _find_executable_with_config("vmrun", configured_executable(PROVIDER_VMWARE_FUSION))


def _parse_parallels_registered_vms(output: str) -> list[RegisteredVm]:
    vms: list[RegisteredVm] = []
    blocks = [block.strip() for block in output.split("\n\n") if block.strip()]
    for block in blocks:
        if block == "INFO":
            continue
        fields: dict[str, str] = {}
        for line in block.splitlines():
            if line.startswith(" ") or ":" not in line:
                continue
            key, value = line.split(":", 1)
            fields[key.strip()] = value.strip()
        vm_id = fields.get("ID")
        if not vm_id:
            continue
        vm_path = _extract_parallels_bundle_path(fields, block)
        if vm_path is None:
            continue
        vms.append(
            RegisteredVm(
                id=vm_id,
                name=fields.get("Name", vm_path.stem),
                path=vm_path,
                status=fields.get("State"),
            )
        )
    return sorted(vms, key=lambda vm: vm.name.lower())


def _extract_parallels_bundle_path(fields: dict[str, str], block: str) -> Path | None:
    for key in ("Home", "Home path"):
        value = fields.get(key, "").strip()
        if value:
            path = _bundle_path_from_string(value)
            if path is not None:
                return path

    match = re.search(r"(/[^'\n]*?\.pvm)(?:/|')", block)
    if match:
        return Path(match.group(1)).expanduser()
    return None


def _bundle_path_from_string(value: str) -> Path | None:
    if ".pvm" not in value.lower():
        return None
    match = re.search(r"(.+?\.pvm)(?:/.*)?$", value)
    if not match:
        return None
    return Path(match.group(1)).expanduser()


def _vmware_inventory_path() -> Path:
    return VMWARE_FUSION_INVENTORY_PATH


def _parse_vmware_inventory(path: Path) -> list[RegisteredVm]:
    records = _parse_vmware_inventory_records(path.read_text(encoding="utf-8"))
    vms: list[RegisteredVm] = []
    for record in records:
        config_path = record.get("config", "").strip()
        if not config_path:
            continue
        vmx_path = Path(config_path).expanduser()
        bundle_path = _vmware_bundle_from_vmx(vmx_path)
        if bundle_path is None:
            continue
        vms.append(
            RegisteredVm(
                id=str(vmx_path),
                name=record.get("DisplayName", bundle_path.stem),
                path=bundle_path,
                status=record.get("State", "normal"),
            )
        )
    return sorted(vms, key=lambda vm: vm.name.lower())


def _parse_vmware_inventory_records(text: str) -> list[dict[str, str]]:
    records: dict[int, dict[str, str]] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"')
        match = re.fullmatch(r"vmlist(\d+)\.(.+)", key)
        if not match:
            continue
        record_id = int(match.group(1))
        field_name = match.group(2)
        records.setdefault(record_id, {})[field_name] = value
    return [records[record_id] for record_id in sorted(records)]


def _register_vmware_inventory_vm(inventory_path: Path, vm_bundle: Path) -> None:
    vmx_path = _find_vmware_vmx(vm_bundle)
    inventory_text = inventory_path.read_text(encoding="utf-8")
    records = _parse_vmware_inventory_records(inventory_text)
    normalized_vmx = _normalize_path(vmx_path)
    for record in records:
        existing_config = record.get("config", "").strip()
        if existing_config and _normalize_path(Path(existing_config)) == normalized_vmx:
            raise RuntimeError(f"{vm_bundle.name} is already present in VMware Fusion inventory.")

    active_records = [record for record in records if record.get("config", "").strip()]
    next_id = _next_vmware_record_id(active_records)
    next_seq = len(active_records)
    display_name = vm_bundle.stem
    uuid_value = _read_vmware_vmx_setting(vmx_path, "uuid.bios")

    new_record = {
        "config": str(vmx_path),
        "DisplayName": display_name,
        "ParentID": "0",
        "ItemID": str(next_id),
        "SeqID": str(next_seq),
        "IsFavorite": "FALSE",
        "IsClone": "FALSE",
        "CfgVersion": "8",
        "State": "normal",
        "IsCfgPathNormalized": "TRUE",
    }
    if uuid_value:
        new_record["UUID"] = uuid_value

    active_records.append(new_record)
    _write_vmware_inventory(inventory_path, active_records)


def _unregister_vmware_inventory_vm(inventory_path: Path, vm_id: str) -> None:
    inventory_text = inventory_path.read_text(encoding="utf-8")
    records = _parse_vmware_inventory_records(inventory_text)
    normalized_id = _normalize_path(Path(vm_id))
    active_records = [record for record in records if record.get("config", "").strip()]
    remaining_records = [
        record
        for record in active_records
        if _normalize_path(Path(record["config"])) != normalized_id
    ]
    if len(remaining_records) == len(active_records):
        raise RuntimeError(f"VMware Fusion inventory entry not found: {vm_id}")
    _write_vmware_inventory(inventory_path, remaining_records)


def _write_vmware_inventory(inventory_path: Path, records: list[dict[str, str]]) -> None:
    inventory_path.parent.mkdir(parents=True, exist_ok=True)
    _backup_timestamped_file(inventory_path)
    lines = ['.encoding = "UTF-8"']
    for record_id, record in enumerate(records, start=1):
        config_path = record["config"]
        vmx_path = Path(config_path).expanduser()
        lines.extend(
            [
                f'vmlist{record_id}.config = "{config_path}"',
                f'vmlist{record_id}.DisplayName = "{record.get("DisplayName", vmx_path.stem)}"',
                f'vmlist{record_id}.ParentID = "0"',
                f'vmlist{record_id}.ItemID = "{record_id}"',
                f'vmlist{record_id}.SeqID = "{record_id - 1}"',
                f'vmlist{record_id}.IsFavorite = "{record.get("IsFavorite", "FALSE")}"',
                f'vmlist{record_id}.IsClone = "{record.get("IsClone", "FALSE")}"',
                f'vmlist{record_id}.CfgVersion = "{record.get("CfgVersion", "8")}"',
                f'vmlist{record_id}.State = "{record.get("State", "normal")}"',
            ]
        )
        uuid_value = record.get("UUID", "").strip()
        if uuid_value:
            lines.append(f'vmlist{record_id}.UUID = "{uuid_value}"')
        lines.append(
            f'vmlist{record_id}.IsCfgPathNormalized = "{record.get("IsCfgPathNormalized", "TRUE")}"'
        )

    for index, record in enumerate(records):
        vmx_path = Path(record["config"]).expanduser()
        guest_value = (
            _read_vmware_vmx_setting(vmx_path, "guestos")
            or record.get("guest")
            or "unknown"
        )
        lines.extend(
            [
                f'index{index}.field0.name = "guest"',
                f'index{index}.field0.value = "{guest_value}"',
                f'index{index}.field0.default = "TRUE"',
                f'index{index}.hostID = "localhost"',
                f'index{index}.id = "{vmx_path}"',
                f'index{index}.field.count = "1"',
            ]
        )
    lines.append(f'index.count = "{len(records)}"')
    lines.append("")
    inventory_path.write_text("\n".join(lines), encoding="utf-8")


def _backup_timestamped_file(path: Path) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = path.with_name(f"{path.name}.{timestamp}.vmhandy-backup")
    counter = 1
    while backup_path.exists():
        backup_path = path.with_name(f"{path.name}.{timestamp}.{counter}.vmhandy-backup")
        counter += 1
    shutil.copy2(path, backup_path)
    return backup_path


def _is_writable_file(path: Path) -> bool:
    return os.access(path, os.W_OK)


def _next_vmware_record_id(records: list[dict[str, str]]) -> int:
    item_ids: list[int] = []
    for record in records:
        raw_item_id = record.get("ItemID", "").strip()
        if raw_item_id.isdigit():
            item_ids.append(int(raw_item_id))
    return max(item_ids, default=0) + 1


def _find_vmware_vmx(vm_bundle: Path) -> Path:
    candidates = sorted(vm_bundle.glob("*.vmx"), key=lambda item: item.name.lower())
    if not candidates:
        raise FileNotFoundError(f"No .vmx file found inside VMware Fusion bundle: {vm_bundle}")
    return candidates[0]


def _vmware_bundle_from_vmx(vmx_path: Path) -> Path | None:
    parent = vmx_path.parent
    if parent.suffix.lower() != ".vmwarevm":
        return None
    return parent


def _read_vmware_vmx_setting(vmx_path: Path, key_name: str) -> str | None:
    if not vmx_path.exists():
        return None
    pattern = re.compile(rf'^{re.escape(key_name)}\s*=\s*"(.+)"\s*$')
    try:
        for line in vmx_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            match = pattern.match(line.strip())
            if match:
                return match.group(1)
    except OSError:
        return None
    return None
