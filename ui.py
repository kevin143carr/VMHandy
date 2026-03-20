from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QObject, QSettings, QThread, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from file_ops import (
    CopyCancelledError,
    PROVIDER_VMWARE_FUSION,
    RegisteredVm,
    VmSelection,
    available_bytes,
    available_provider_names,
    can_write_to_file,
    can_write_to_folder,
    compute_total_size,
    copy_tree_with_progress,
    default_provider_name,
    ensure_vm_bundle,
    get_provider,
    list_vm_bundles,
    provider_label,
    remove_tree,
    vm_bundle_suffix,
    VMWARE_FUSION_INVENTORY_PATH,
)


@dataclass(slots=True)
class PendingAction:
    label: str
    runner: Callable[[], None]
    cancel: Callable[[], None] | None = None
    is_copy: bool = False


class Worker(QObject):
    progress = Signal(object, object, str)
    finished = Signal(str)
    cancelled = Signal(str)
    failed = Signal(str)

    def __init__(self, action: PendingAction) -> None:
        super().__init__()
        self._action = action

    def run(self) -> None:
        try:
            self._action.runner()
        except CopyCancelledError as exc:
            self.cancelled.emit(str(exc) or "Copy cancelled.")
            return
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc))
            return
        self.finished.emit(self._action.label)


class VmHandyWindow(QMainWindow):
    ORGANIZATION_NAME = "KevinCarr"
    APPLICATION_NAME = "VMHandy"
    SETTINGS_DEFAULTS = {
        "provider": "",
        "source_folder": "",
        "local_folder": "",
        "source_vm_name": "",
        "local_vm_name": "",
        "registered_vm_id": "",
    }

    def __init__(self) -> None:
        super().__init__()
        self.resize(1180, 680)

        self.settings = QSettings(self.ORGANIZATION_NAME, self.APPLICATION_NAME)
        self._migrate_legacy_settings()
        self._registered_vms: list[RegisteredVm] = []
        self._thread: QThread | None = None
        self._worker: Worker | None = None
        self._cancel_current_action: Callable[[], None] | None = None
        self._copy_in_progress = False
        self._cancellation_requested = False
        self._close_requested = False

        self.provider_combo = QComboBox()
        self._populate_provider_combo()

        self.source_folder_input = QLineEdit()
        self.source_folder_input.setPlaceholderText("Choose the remote VM folder")
        self.local_folder_input = QLineEdit()
        self.local_folder_input.setPlaceholderText("Choose a local destination folder")

        self.source_list_label = QLabel("Remote folder VMs")
        self.local_list_label = QLabel("Local folder VMs")
        self.registered_list_label = QLabel("Registered VMs")
        self.source_vm_list = QListWidget()
        self.local_vm_list = QListWidget()
        self.registered_vm_list = QListWidget()
        self.source_space_label = QLabel("Available: No folder selected")
        self.local_space_label = QLabel("Available: No folder selected")

        self.source_vm_list.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        self.local_vm_list.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        self.registered_vm_list.setSelectionMode(QListWidget.SelectionMode.SingleSelection)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.status_label = QLabel("Idle")
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.log_output = QPlainTextEdit()
        self.log_output.setReadOnly(True)
        self.log_output.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        self.copy_button = QPushButton("Copy To Local")
        self.delete_button = QPushButton("Delete")
        self.replace_button = QPushButton("Copy VM To Remote")
        self.register_button = QPushButton("Register")
        self.unregister_button = QPushButton("Unregister")
        self.refresh_button = QPushButton("Refresh Lists")

        self.copy_button.clicked.connect(self.copy_to_local)
        self.delete_button.clicked.connect(self.delete_selected_vm)
        self.replace_button.clicked.connect(self.copy_to_remote)
        self.register_button.clicked.connect(self.register_selected_vm)
        self.unregister_button.clicked.connect(self.unregister_selected_vm)
        self.refresh_button.clicked.connect(self.refresh_or_cancel)
        self.provider_combo.currentIndexChanged.connect(self._on_provider_changed)
        self.source_vm_list.itemSelectionChanged.connect(self._on_source_selection_changed)
        self.local_vm_list.itemSelectionChanged.connect(self._on_local_selection_changed)
        self.registered_vm_list.itemSelectionChanged.connect(self._on_registered_selection_changed)
        self.source_folder_input.editingFinished.connect(self._on_source_folder_changed)
        self.local_folder_input.editingFinished.connect(self._on_local_folder_changed)

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.addWidget(self._build_paths_group())
        layout.addWidget(self._build_vm_lists_group())
        layout.addWidget(self._build_actions_group())
        layout.addWidget(self._build_status_group())
        self.setCentralWidget(central)

        self._append_log("Select a provider, remote folder, and local folder to manage VM bundles.")
        self._restore_settings()
        self._update_window_title()
        self._update_action_states()

    def _build_paths_group(self) -> QGroupBox:
        group = QGroupBox("Paths")
        layout = QGridLayout(group)

        self.source_browse_button = QPushButton("Browse Remote Folder")
        self.source_browse_button.clicked.connect(self.choose_source_folder)
        self.local_browse_button = QPushButton("Browse Folder")
        self.local_browse_button.clicked.connect(self.choose_local_folder)

        layout.addWidget(QLabel("Provider"), 0, 0)
        layout.addWidget(self.provider_combo, 0, 1, 1, 2)
        layout.addWidget(QLabel("Remote VM folder"), 1, 0)
        layout.addWidget(self.source_folder_input, 1, 1)
        layout.addWidget(self.source_browse_button, 1, 2)
        layout.addWidget(QLabel("Local destination"), 2, 0)
        layout.addWidget(self.local_folder_input, 2, 1)
        layout.addWidget(self.local_browse_button, 2, 2)
        return group

    def _build_vm_lists_group(self) -> QGroupBox:
        group = QGroupBox("VM Bundles")
        layout = QGridLayout(group)
        layout.addWidget(self.source_list_label, 0, 0)
        layout.addWidget(self.local_list_label, 0, 1)
        layout.addWidget(self.registered_list_label, 0, 2)
        layout.addWidget(self.source_vm_list, 1, 0)
        layout.addWidget(self.local_vm_list, 1, 1)
        layout.addWidget(self.registered_vm_list, 1, 2)
        layout.addWidget(self.source_space_label, 2, 0)
        layout.addWidget(self.local_space_label, 2, 1)
        layout.addWidget(QLabel(""), 2, 2)
        return group

    def _build_actions_group(self) -> QGroupBox:
        group = QGroupBox("Actions")
        layout = QHBoxLayout(group)
        layout.addWidget(self.copy_button)
        layout.addWidget(self.replace_button)
        layout.addWidget(self.register_button)
        layout.addWidget(self.unregister_button)
        layout.addWidget(self.delete_button)
        layout.addWidget(self.refresh_button)
        return group

    def _build_status_group(self) -> QGroupBox:
        group = QGroupBox("Status")
        layout = QVBoxLayout(group)
        layout.addWidget(self.progress_bar)
        layout.addWidget(self.status_label)
        layout.addWidget(self.log_output)
        return group

    def _populate_provider_combo(self) -> None:
        selected = self._setting("provider", default_provider_name())
        for name in available_provider_names():
            provider = get_provider(name)
            label = provider_label(name)
            if not provider.is_available():
                label = f"{label} (Unavailable)"
            self.provider_combo.addItem(label, name)
        index = self.provider_combo.findData(selected)
        self.provider_combo.setCurrentIndex(index if index >= 0 else 0)

    def choose_source_folder(self) -> None:
        selected = QFileDialog.getExistingDirectory(self, "Choose Remote VM Folder")
        if selected:
            self.source_folder_input.setText(selected)
            self._on_source_folder_changed()

    def choose_local_folder(self) -> None:
        selected = QFileDialog.getExistingDirectory(self, "Choose Local Destination Folder")
        if selected:
            self.local_folder_input.setText(selected)
            self._on_local_folder_changed()

    def current_provider_name(self) -> str:
        return str(self.provider_combo.currentData() or default_provider_name())

    def current_provider(self):
        return get_provider(self.current_provider_name())

    def _update_window_title(self) -> None:
        provider = self.current_provider()
        self.setWindowTitle(f"VMHandy [{provider.label}]")

    def refresh_or_cancel(self) -> None:
        if self._copy_in_progress:
            self.cancel_current_action()
            return
        self.refresh_vm_lists()

    def refresh_vm_lists(self) -> None:
        source_folder = self._folder_path(self.source_folder_input)
        local_folder = self._folder_path(self.local_folder_input)
        provider = self.current_provider()
        self._set_list_labels(source_folder, local_folder, provider.label)
        self._set_space_label(self.source_space_label, source_folder)
        self._set_space_label(self.local_space_label, local_folder)
        self._populate_bundle_list(
            self.source_vm_list,
            source_folder,
            self._setting("source_vm_name"),
        )
        self._populate_bundle_list(
            self.local_vm_list,
            local_folder,
            self._setting("local_vm_name"),
        )
        self._populate_registered_vm_list(
            provider,
            self._setting("registered_vm_id"),
        )
        self._update_action_states()

    def _set_list_labels(self, source_folder: Path | None, local_folder: Path | None, provider_name: str) -> None:
        source_text = str(source_folder) if source_folder is not None else "No folder selected"
        local_text = str(local_folder) if local_folder is not None else "No folder selected"
        bundle_suffix = vm_bundle_suffix(self.current_provider_name())
        self.source_list_label.setText(f"Remote folder VMs ({bundle_suffix}): {source_text}")
        self.local_list_label.setText(f"Local folder VMs ({bundle_suffix}): {local_text}")
        self.registered_list_label.setText(f"Registered {provider_name} VMs")

    def _set_space_label(self, label: QLabel, folder: Path | None) -> None:
        if folder is None:
            label.setText("Available: No folder selected")
            return
        try:
            free_space = available_bytes(folder)
        except Exception as exc:  # noqa: BLE001
            label.setText(f"Available: Unable to read ({exc})")
            return
        label.setText(f"Available: {self._format_bytes(free_space)}")

    def _populate_bundle_list(self, widget: QListWidget, folder: Path | None, preferred_name: str = "") -> None:
        current_name = preferred_name or self._selected_vm_name(widget)
        widget.clear()
        if folder is None:
            return
        try:
            bundles = list_vm_bundles(folder, self.current_provider_name())
        except Exception as exc:  # noqa: BLE001
            self._append_log(f"Unable to scan {folder}: {exc}")
            return

        for bundle in bundles:
            size_text = self._format_bytes(compute_total_size(bundle))
            item = QListWidgetItem(f"{bundle.name} ({size_text})")
            item.setData(Qt.ItemDataRole.UserRole, str(bundle))
            item.setToolTip(str(bundle))
            widget.addItem(item)
            if bundle.name == current_name:
                widget.setCurrentItem(item)

    def _populate_registered_vm_list(self, provider, preferred_id: str = "") -> None:
        current_id = preferred_id or self._selected_registered_vm_id()
        self.registered_vm_list.clear()
        self._registered_vms = []

        if not provider.is_available():
            self._append_log(f"{provider.label} is not available on this Mac.")
            return

        try:
            self._registered_vms = provider.list_registered_vms()
        except Exception as exc:  # noqa: BLE001
            self._append_log(f"Unable to read {provider.label} VM inventory: {exc}")
            return

        for vm in self._registered_vms:
            status = f" [{vm.status}]" if vm.status else ""
            item = QListWidgetItem(f"{vm.name}{status}")
            item.setData(Qt.ItemDataRole.UserRole, vm.id)
            item.setToolTip(str(vm.path))
            self.registered_vm_list.addItem(item)
            if vm.id == current_id:
                self.registered_vm_list.setCurrentItem(item)

    def _folder_path(self, field: QLineEdit) -> Path | None:
        text = field.text().strip()
        return Path(text).expanduser() if text else None

    def _selected_vm_name(self, widget: QListWidget) -> str | None:
        path = self._selected_vm_path(widget)
        return path.name if path is not None else None

    def _selected_vm_path(self, widget: QListWidget) -> Path | None:
        selected_items = widget.selectedItems()
        item = selected_items[0] if selected_items else None
        if item is None:
            return None
        return Path(item.data(Qt.ItemDataRole.UserRole))

    def _selected_registered_vm_id(self) -> str | None:
        selected_items = self.registered_vm_list.selectedItems()
        item = selected_items[0] if selected_items else None
        return str(item.data(Qt.ItemDataRole.UserRole)) if item is not None else None

    def _selected_registered_vm(self) -> RegisteredVm | None:
        selected_id = self._selected_registered_vm_id()
        if selected_id is None:
            return None
        for vm in self._registered_vms:
            if vm.id == selected_id:
                return vm
        return None

    def current_selection(self) -> VmSelection:
        local_parent = Path(self.local_folder_input.text()).expanduser()
        source_vm = self._selected_vm_path(self.source_vm_list)
        provider_name = self.current_provider_name()

        if not self.source_folder_input.text().strip():
            raise ValueError("Choose a remote folder first.")
        if not self.local_folder_input.text().strip():
            raise ValueError("Choose a local destination folder first.")
        if source_vm is None:
            raise ValueError("Choose a VM from the remote folder list first.")
        if not local_parent.exists():
            raise FileNotFoundError(f"Local destination folder does not exist: {local_parent}")

        ensure_vm_bundle(source_vm, provider_name)
        return VmSelection(source_vm=source_vm, local_parent=local_parent)

    def copy_to_local(self) -> None:
        selection = self._guard_selection()
        if selection is None:
            return

        local_vm = selection.local_vm
        self._start_copy_action(
            source=selection.source_vm,
            destination=local_vm,
            destination_folder=selection.local_parent,
            permission_error=f"No write permission for local destination: {selection.local_parent}",
            not_enough_space_error="Not enough free space on the local drive for this VM.",
            overwrite_title="Overwrite Local VM",
            overwrite_message=f"Overwrite the existing local VM?\n\n{local_vm}",
            start_message=f"Copy {selection.source_vm.name} from remote to local folder.",
            overwrite_message_log=f"Overwrite local VM {local_vm.name} with the remote copy.",
            completed_label="Copy to local completed",
        )

    def delete_selected_vm(self) -> None:
        remote_vm = self._selected_vm_path(self.source_vm_list)
        local_vm = self._selected_vm_path(self.local_vm_list)
        source_folder = self._folder_path(self.source_folder_input)
        local_folder = self._folder_path(self.local_folder_input)
        if remote_vm is not None:
            self._start_delete_action(
                target_vm=remote_vm,
                target_folder=source_folder,
                missing_error="There is no remote VM to delete.",
                permission_error=f"No write permission for remote destination: {source_folder}",
                confirm_title="Delete Remote VM",
                confirm_message=f"Delete remote VM?\n\n{remote_vm}",
                action_text=f"delete remote VM {remote_vm.name}",
                completed_label="Delete remote VM completed",
            )
            return

        provider = self.current_provider()
        registered_local_vm = self._find_registered_local_vm(local_vm)
        if registered_local_vm is not None:
            if not provider.supports_unregistration:
                self._show_error(
                    f"{local_vm.name} is registered in {provider.label} and must be unregistered first, "
                    f"but {provider.label} unregister is not supported yet."
                )
                return
            confirmed = self._show_confirmation(
                title="Unregister And Delete Local Copy",
                message=(
                    f"{local_vm.name} is registered in {provider.label}.\n\n"
                    "Delete will unregister it first and then remove the local VM bundle.\n\n"
                    f"VM: {local_vm}"
                ),
            )
            if confirmed != QMessageBox.StandardButton.Yes:
                return

            self._append_log(
                f"About to: unregister {registered_local_vm.name} from {provider.label} and delete the local copy."
            )
            self._run_action(
                PendingAction(
                    label="Unregister and delete local copy completed",
                    runner=lambda: self._unregister_and_delete_local_vm(provider, registered_local_vm, local_vm),
                )
            )
            return

        self._start_delete_action(
            target_vm=local_vm,
            target_folder=local_folder,
            missing_error="There is no local VM copy to delete.",
            permission_error=f"No write permission for local destination: {local_folder}",
            confirm_title="Delete Local Copy",
            confirm_message=f"Delete local VM copy?\n\n{local_vm}",
            action_text=f"delete local VM {local_vm.name}" if local_vm is not None else "delete local VM",
            completed_label="Delete local copy completed",
        )

    def copy_to_remote(self) -> None:
        local_vm = self._selected_vm_path(self.local_vm_list)
        source_folder = self._folder_path(self.source_folder_input)
        if source_folder is None:
            self._show_error("Choose a remote folder first.")
            return
        if local_vm is None or not local_vm.exists():
            self._show_error("There is no local VM copy to push back to the remote drive.")
            return
        if not can_write_to_folder(source_folder):
            self._show_error(f"No write permission for remote destination: {source_folder}")
            return

        destination_vm = source_folder / local_vm.name
        self._start_copy_action(
            source=local_vm,
            destination=destination_vm,
            destination_folder=source_folder,
            permission_error=f"No write permission for remote destination: {source_folder}",
            not_enough_space_error="Not enough free space on the remote drive to replace the VM.",
            overwrite_title="Overwrite Remote VM",
            overwrite_message=f"Overwrite the existing remote VM?\n\n{destination_vm}",
            start_message=f"Copy local VM {local_vm.name} to remote folder.",
            overwrite_message_log=f"Overwrite remote VM {destination_vm.name} with the local copy.",
            completed_label="Copy VM to remote completed",
        )

    def register_selected_vm(self) -> None:
        provider = self.current_provider()
        local_vm = self._selected_vm_path(self.local_vm_list)
        if local_vm is None or not local_vm.exists():
            self._show_error("Choose a local VM bundle to register first.")
            return
        if not provider.is_available():
            self._show_error(f"{provider.label} is not available on this Mac.")
            return
        if not provider.supports_registration:
            self._show_error(self._registration_unavailable_message(provider))
            return

        try:
            existing = provider.find_registered_vm(local_vm)
        except Exception as exc:  # noqa: BLE001
            self._show_error(str(exc))
            return
        if existing is not None:
            self._show_error(f"{local_vm.name} is already registered in {provider.label}.")
            return

        self._append_log(f"About to: register {local_vm.name} in {provider.label}.")
        self._run_action(
            PendingAction(
                label="Register VM completed",
                runner=lambda: self._register_local_vm(provider.name, provider, local_vm),
            )
        )

    def unregister_selected_vm(self) -> None:
        provider = self.current_provider()
        registered_vm = self._selected_registered_vm()
        if registered_vm is None:
            self._show_error("Choose a registered VM first.")
            return
        if not provider.is_available():
            self._show_error(f"{provider.label} is not available on this Mac.")
            return
        if not provider.supports_unregistration:
            self._show_error(self._unregistration_unavailable_message(provider))
            return

        confirmed = self._show_confirmation(
            title=f"Unregister {provider.label} VM",
            message=f"Unregister this VM from {provider.label}?\n\n{registered_vm.name}\n{registered_vm.path}",
        )
        if confirmed != QMessageBox.StandardButton.Yes:
            return

        self._append_log(f"About to: unregister {registered_vm.name} from {provider.label}.")
        self._run_action(
            PendingAction(
                label="Unregister VM completed",
                runner=lambda: provider.unregister_vm(registered_vm.id),
            )
        )

    def _guard_selection(self) -> VmSelection | None:
        try:
            return self.current_selection()
        except Exception as exc:  # noqa: BLE001
            self._show_error(str(exc))
            return None

    def _start_copy_action(
        self,
        *,
        source: Path,
        destination: Path,
        destination_folder: Path,
        permission_error: str,
        not_enough_space_error: str,
        overwrite_title: str,
        overwrite_message: str,
        start_message: str,
        overwrite_message_log: str,
        completed_label: str,
    ) -> None:
        if not can_write_to_folder(destination_folder):
            self._show_error(permission_error)
            return

        if not self._has_enough_space(source, destination, destination_folder):
            self._show_error(not_enough_space_error)
            return

        overwrite = self._confirm_overwrite(
            destination=destination,
            title=overwrite_title,
            message=overwrite_message,
        )
        if overwrite is None:
            return

        action_text = overwrite_message_log if overwrite else start_message
        cancel_event = threading.Event()
        self._append_log(f"About to: {action_text}")
        self._run_action(
            PendingAction(
                label=completed_label,
                runner=lambda: copy_tree_with_progress(
                    source,
                    destination,
                    self._progress_callback,
                    overwrite=overwrite,
                    should_cancel=cancel_event.is_set,
                    provider_name=self.current_provider_name(),
                ),
                cancel=cancel_event.set,
                is_copy=True,
            )
        )

    def _start_delete_action(
        self,
        *,
        target_vm: Path | None,
        target_folder: Path | None,
        missing_error: str,
        permission_error: str,
        confirm_title: str,
        confirm_message: str,
        action_text: str,
        completed_label: str,
    ) -> None:
        if target_vm is None or target_folder is None or not target_vm.exists():
            self._show_error(missing_error)
            return
        if not can_write_to_folder(target_folder):
            self._show_error(permission_error)
            return

        confirmed = self._show_confirmation(title=confirm_title, message=confirm_message)
        if confirmed != QMessageBox.StandardButton.Yes:
            return

        self._append_log(f"About to: {action_text}.")
        self._run_action(
            PendingAction(
                label=completed_label,
                runner=lambda: remove_tree(target_vm),
            )
        )

    def _has_enough_space(self, source: Path, destination: Path, destination_folder: Path) -> bool:
        required_bytes = compute_total_size(source)
        free_space = available_bytes(destination_folder)
        if destination.exists():
            free_space += compute_total_size(destination)
        return required_bytes <= free_space

    def _confirm_overwrite(self, *, destination: Path, title: str, message: str) -> bool | None:
        if not destination.exists():
            return False
        confirmed = self._show_message_box(
            icon=QMessageBox.Icon.Question,
            title=title,
            message=message,
            buttons=QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            default_button=QMessageBox.StandardButton.No,
        )
        if confirmed != QMessageBox.StandardButton.Yes:
            return None
        return True

    def cancel_current_action(self) -> None:
        if self._cancel_current_action is None or self._cancellation_requested:
            return
        self._cancellation_requested = True
        self._append_log("Cancellation requested.")
        self.status_label.setText("Cancelling...")
        self._cancel_current_action()
        self._update_action_states()

    def _run_action(self, action: PendingAction) -> None:
        if self._thread is not None and self._thread.isRunning():
            self._show_error("Another action is already running.")
            return

        self._cancel_current_action = action.cancel
        self._copy_in_progress = action.is_copy
        self._cancellation_requested = False
        self.progress_bar.setValue(0)
        self.status_label.setText("Working...")
        self._update_action_states()
        self._append_log(action.label.replace(" completed", " started"))

        self._thread = QThread(self)
        self._worker = Worker(action)
        self._worker.moveToThread(self._thread)

        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished.connect(self._on_finished)
        self._worker.cancelled.connect(self._on_cancelled)
        self._worker.failed.connect(self._on_failed)
        self._worker.finished.connect(self._thread.quit)
        self._worker.cancelled.connect(self._thread.quit)
        self._worker.failed.connect(self._thread.quit)
        self._thread.finished.connect(self._cleanup_thread)
        self._thread.finished.connect(self.refresh_vm_lists)
        self._thread.start()

    def _progress_callback(self, copied_bytes: int, total_bytes: int, current_item: str) -> None:
        if self._worker is not None:
            self._worker.progress.emit(copied_bytes, total_bytes, current_item)

    def _on_progress(self, copied_bytes: object, total_bytes: object, current_item: str) -> None:
        copied_value = int(copied_bytes)
        total_value = int(total_bytes)
        percent = 100 if total_value == 0 else int((copied_value / total_value) * 100)
        self.progress_bar.setValue(percent)
        self.status_label.setText(f"{percent}% - {current_item}")

    def _on_finished(self, label: str) -> None:
        self.progress_bar.setValue(100)
        self.status_label.setText(label)
        self._append_log(label)
        if self.current_provider_name() == PROVIDER_VMWARE_FUSION and label in {
            "Register VM completed",
            "Unregister VM completed",
            "Unregister and delete local copy completed",
        }:
            self._append_log("VMware Fusion may need to be restarted if its library does not refresh automatically.")

    def _on_cancelled(self, message: str) -> None:
        self.progress_bar.setValue(0)
        self.status_label.setText(message)
        self._append_log(message)

    def _on_failed(self, message: str) -> None:
        self.status_label.setText("Action failed")
        self._append_log(f"Error: {message}")
        self._show_error(message)

    def _cleanup_thread(self) -> None:
        if self._worker is not None:
            self._worker.deleteLater()
        if self._thread is not None:
            self._thread.deleteLater()
        self._cancel_current_action = None
        self._copy_in_progress = False
        self._cancellation_requested = False
        self._worker = None
        self._thread = None
        self._update_action_states()
        if self._close_requested:
            self.close()

    def _show_error(self, message: str) -> None:
        self._show_message_box(
            icon=QMessageBox.Icon.Critical,
            title="VMHandy",
            message=message,
            buttons=QMessageBox.StandardButton.Ok,
        )

    def _show_confirmation(self, *, title: str, message: str) -> QMessageBox.StandardButton:
        return self._show_message_box(
            icon=QMessageBox.Icon.Question,
            title=title,
            message=message,
            buttons=QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            default_button=QMessageBox.StandardButton.No,
        )

    def _show_message_box(
        self,
        *,
        icon: QMessageBox.Icon,
        title: str,
        message: str,
        buttons: QMessageBox.StandardButton,
        default_button: QMessageBox.StandardButton | None = None,
    ) -> QMessageBox.StandardButton:
        message_box = QMessageBox(self)
        message_box.setWindowFlag(Qt.WindowType.Sheet, False)
        message_box.setWindowModality(Qt.WindowModality.WindowModal)
        message_box.setIcon(icon)
        message_box.setWindowTitle(title)
        message_box.setText(message)
        message_box.setStandardButtons(buttons)
        if default_button is not None:
            message_box.setDefaultButton(default_button)
        QTimer.singleShot(0, lambda: self._center_dialog(message_box))
        return QMessageBox.StandardButton(message_box.exec())

    def _center_dialog(self, dialog: QMessageBox) -> None:
        dialog.adjustSize()
        dialog_rect = dialog.frameGeometry()
        dialog_rect.moveCenter(self.frameGeometry().center())
        dialog.move(dialog_rect.topLeft())

    def _append_log(self, message: str) -> None:
        self.log_output.appendPlainText(message)

    def _sync_refresh_button(self) -> None:
        if self._copy_in_progress:
            self.refresh_button.setText("Cancel")
            return
        self.refresh_button.setText("Refresh Lists")

    def _format_bytes(self, size: int) -> str:
        units = ["B", "KB", "MB", "GB", "TB"]
        value = float(size)
        for unit in units:
            if value < 1024 or unit == units[-1]:
                if unit == "B":
                    return f"{int(value)} {unit}"
                return f"{value:.1f} {unit}"
            value /= 1024
        return f"{size} B"

    def _update_action_states(self) -> None:
        provider = self.current_provider()
        source_folder = self._folder_path(self.source_folder_input)
        local_folder = self._folder_path(self.local_folder_input)
        source_vm = self._selected_vm_path(self.source_vm_list)
        local_vm = self._selected_vm_path(self.local_vm_list)
        source_selected = bool(self.source_vm_list.selectedItems())
        local_selected = bool(self.local_vm_list.selectedItems())
        registered_selected = bool(self.registered_vm_list.selectedItems())
        action_running = self._thread is not None and self._thread.isRunning()
        cancellation_pending = self._cancellation_requested

        copy_enabled = (
            not action_running
            and source_selected
            and source_folder is not None
            and local_folder is not None
            and source_vm is not None
            and local_folder.exists()
            and can_write_to_folder(local_folder)
        )
        delete_enabled = (
            not action_running
            and (
                (source_selected and source_folder is not None and can_write_to_folder(source_folder))
                or (local_selected and local_folder is not None and can_write_to_folder(local_folder))
            )
        )
        replace_enabled = (
            not action_running
            and local_selected
            and local_vm is not None
            and source_folder is not None
            and source_folder.exists()
            and can_write_to_folder(source_folder)
        )
        register_enabled = (
            not action_running
            and local_selected
            and local_vm is not None
            and provider.is_available()
            and provider.supports_registration
            and not self._is_registered_locally(local_vm)
        )
        unregister_enabled = (
            not action_running
            and registered_selected
            and provider.is_available()
            and provider.supports_unregistration
        )
        refresh_enabled = (self._copy_in_progress and not cancellation_pending) or not action_running
        controls_locked = self._copy_in_progress

        if controls_locked:
            self.copy_button.setEnabled(False)
            self.delete_button.setEnabled(False)
            self.replace_button.setEnabled(False)
            self.register_button.setEnabled(False)
            self.unregister_button.setEnabled(False)
            self.refresh_button.setEnabled(not cancellation_pending)
            self.provider_combo.setEnabled(False)
            self.source_folder_input.setEnabled(False)
            self.local_folder_input.setEnabled(False)
            self.source_browse_button.setEnabled(False)
            self.local_browse_button.setEnabled(False)
            self.source_vm_list.setEnabled(False)
            self.local_vm_list.setEnabled(False)
            self.registered_vm_list.setEnabled(False)
            self._sync_refresh_button()
            self.copy_button.setText("Copy To Local")
            self.replace_button.setText("Copy VM To Remote")
            self.register_button.setText(f"Register in {provider.label}")
            self.unregister_button.setText(f"Unregister from {provider.label}")
            self.delete_button.setText("Delete")
            return

        self.copy_button.setEnabled(copy_enabled)
        self.delete_button.setEnabled(delete_enabled)
        self.replace_button.setEnabled(replace_enabled)
        self.register_button.setEnabled(register_enabled)
        self.unregister_button.setEnabled(unregister_enabled)
        self.refresh_button.setEnabled(refresh_enabled)
        self.provider_combo.setEnabled(not controls_locked)
        self.source_folder_input.setEnabled(not controls_locked)
        self.local_folder_input.setEnabled(not controls_locked)
        self.source_browse_button.setEnabled(not controls_locked)
        self.local_browse_button.setEnabled(not controls_locked)
        self.source_vm_list.setEnabled(not controls_locked)
        self.local_vm_list.setEnabled(not controls_locked)
        self.registered_vm_list.setEnabled(not controls_locked)
        self._sync_refresh_button()

        self.register_button.setText(f"Register in {provider.label}")
        self.unregister_button.setText(f"Unregister from {provider.label}")

        if source_selected:
            self.delete_button.setText("Delete Remote VM")
        elif local_selected:
            self.delete_button.setText("Delete Local Copy")
        else:
            self.delete_button.setText("Delete")

    def _is_registered_locally(self, local_vm: Path) -> bool:
        normalized = local_vm.expanduser().resolve(strict=False)
        return any(vm.path.expanduser().resolve(strict=False) == normalized for vm in self._registered_vms)

    def _find_registered_local_vm(self, local_vm: Path | None) -> RegisteredVm | None:
        if local_vm is None:
            return None
        normalized = local_vm.expanduser().resolve(strict=False)
        for vm in self._registered_vms:
            if vm.path.expanduser().resolve(strict=False) == normalized:
                return vm
        return None

    def _unregister_and_delete_local_vm(self, provider, registered_vm: RegisteredVm, local_vm: Path) -> None:
        provider.unregister_vm(registered_vm.id)
        remove_tree(local_vm)

    def _register_local_vm(self, provider_name: str, provider, local_vm: Path) -> None:
        ensure_vm_bundle(local_vm, provider_name)
        provider.register_vm(local_vm)

    def _registration_unavailable_message(self, provider) -> str:
        if provider.name == PROVIDER_VMWARE_FUSION:
            if not VMWARE_FUSION_INVENTORY_PATH.exists():
                return f"VMware Fusion inventory file does not exist: {VMWARE_FUSION_INVENTORY_PATH}"
            if not can_write_to_file(VMWARE_FUSION_INVENTORY_PATH):
                return f"VMware Fusion inventory file is not writable: {VMWARE_FUSION_INVENTORY_PATH}"
        return f"{provider.label} registration is not supported yet."

    def _unregistration_unavailable_message(self, provider) -> str:
        if provider.name == PROVIDER_VMWARE_FUSION:
            if not VMWARE_FUSION_INVENTORY_PATH.exists():
                return f"VMware Fusion inventory file does not exist: {VMWARE_FUSION_INVENTORY_PATH}"
            if not can_write_to_file(VMWARE_FUSION_INVENTORY_PATH):
                return f"VMware Fusion inventory file is not writable: {VMWARE_FUSION_INVENTORY_PATH}"
        return f"{provider.label} unregister is not supported yet."

    def _on_source_folder_changed(self) -> None:
        self._handle_folder_change(
            field=self.source_folder_input,
            folder_key="source_folder",
            selection_key="source_vm_name",
        )

    def _on_local_folder_changed(self) -> None:
        self._handle_folder_change(
            field=self.local_folder_input,
            folder_key="local_folder",
            selection_key="local_vm_name",
        )

    def _on_provider_changed(self) -> None:
        self._set_setting("provider", self.current_provider_name())
        self._set_setting("registered_vm_id", "")
        self._update_window_title()
        self.refresh_vm_lists()

    def _on_source_selection_changed(self) -> None:
        self._handle_selection_change(
            selected_widget=self.source_vm_list,
            other_widgets=[self.local_vm_list, self.registered_vm_list],
            selected_key="source_vm_name",
            cleared_keys=["local_vm_name", "registered_vm_id"],
        )

    def _on_local_selection_changed(self) -> None:
        self._handle_selection_change(
            selected_widget=self.local_vm_list,
            other_widgets=[self.source_vm_list, self.registered_vm_list],
            selected_key="local_vm_name",
            cleared_keys=["source_vm_name", "registered_vm_id"],
        )

    def _on_registered_selection_changed(self) -> None:
        self._handle_selection_change(
            selected_widget=self.registered_vm_list,
            other_widgets=[self.source_vm_list, self.local_vm_list],
            selected_key="registered_vm_id",
            cleared_keys=["source_vm_name", "local_vm_name"],
            selected_value_getter=self._selected_registered_vm_id,
        )

    def _handle_folder_change(self, *, field: QLineEdit, folder_key: str, selection_key: str) -> None:
        self._set_setting(folder_key, field.text().strip())
        self._set_setting(selection_key, "")
        self.refresh_vm_lists()

    def _handle_selection_change(
        self,
        *,
        selected_widget: QListWidget,
        other_widgets: list[QListWidget],
        selected_key: str,
        cleared_keys: list[str],
        selected_value_getter: Callable[[], str | None] | None = None,
    ) -> None:
        selected_value = (
            selected_value_getter() if selected_value_getter is not None else self._selected_vm_name(selected_widget)
        ) or ""
        if selected_value:
            for widget in other_widgets:
                widget.blockSignals(True)
                widget.clearSelection()
                widget.blockSignals(False)
            for key in cleared_keys:
                self._set_setting(key, "")
        self._set_setting(selected_key, selected_value)
        self._update_action_states()

    def _restore_settings(self) -> None:
        self.source_folder_input.setText(self._setting("source_folder"))
        self.local_folder_input.setText(self._setting("local_folder"))
        provider = self._setting("provider", default_provider_name())
        index = self.provider_combo.findData(provider)
        self.provider_combo.setCurrentIndex(index if index >= 0 else 0)
        self.refresh_vm_lists()

    def closeEvent(self, event) -> None:  # noqa: N802
        if self._thread is not None and self._thread.isRunning():
            self._close_requested = True
            self.cancel_current_action()
            event.ignore()
            return

        self._set_setting("provider", self.current_provider_name())
        self._set_setting("source_folder", self.source_folder_input.text().strip())
        self._set_setting("local_folder", self.local_folder_input.text().strip())
        self._set_setting("source_vm_name", self._selected_vm_name(self.source_vm_list) or "")
        self._set_setting("local_vm_name", self._selected_vm_name(self.local_vm_list) or "")
        self._set_setting("registered_vm_id", self._selected_registered_vm_id() or "")
        self._close_requested = False
        super().closeEvent(event)

    def _setting(self, key: str, default: str = "") -> str:
        value = self.settings.value(key, default)
        if value is None:
            return default
        return str(value)

    def _set_setting(self, key: str, value: str) -> None:
        self.settings.setValue(key, value)
        self.settings.sync()

    def _has_saved_settings(self) -> bool:
        return any(self.settings.contains(key) for key in self.SETTINGS_DEFAULTS)

    def _migrate_legacy_settings(self) -> None:
        legacy_path = Path(__file__).resolve().parent / "vmhandy.ini"
        if self._has_saved_settings() or not legacy_path.exists():
            return

        migrated = False
        for line in legacy_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or line.startswith(";"):
                continue
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if key not in self.SETTINGS_DEFAULTS:
                continue
            self.settings.setValue(key, value.strip())
            migrated = True

        if migrated:
            self.settings.sync()
