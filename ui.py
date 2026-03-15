from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QObject, QThread, Qt, Signal
from PySide6.QtWidgets import (
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QListWidget,
    QListWidgetItem,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QPlainTextEdit,
    QProgressBar,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from file_ops import (
    VmSelection,
    available_bytes,
    can_write_to_folder,
    compute_total_size,
    copy_tree_with_progress,
    ensure_pvm,
    list_vm_bundles,
    remove_tree,
    replace_tree,
)


@dataclass(slots=True)
class PendingAction:
    label: str
    runner: Callable[[], None]


class Worker(QObject):
    progress = Signal(object, object, str)
    finished = Signal(str)
    failed = Signal(str)

    def __init__(self, action: PendingAction) -> None:
        super().__init__()
        self._action = action

    def run(self) -> None:
        try:
            self._action.runner()
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc))
            return
        self.finished.emit(self._action.label)


class VmHandyWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("VMHandy")
        self.resize(980, 640)

        self.settings_path = Path(__file__).resolve().parent / "vmhandy.ini"
        self.settings = self._load_settings()
        self._thread: QThread | None = None
        self._worker: Worker | None = None

        self.source_folder_input = QLineEdit()
        self.source_folder_input.setPlaceholderText("Choose the remote VM folder")
        self.local_folder_input = QLineEdit()
        self.local_folder_input.setPlaceholderText("Choose a local destination folder")
        self.source_list_label = QLabel("Remote folder VMs")
        self.local_list_label = QLabel("Local folder VMs")
        self.source_vm_list = QListWidget()
        self.local_vm_list = QListWidget()
        self.source_vm_list.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        self.local_vm_list.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.status_label = QLabel("Idle")
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.log_output = QPlainTextEdit()
        self.log_output.setReadOnly(True)
        self.log_output.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        self.copy_button = QPushButton("Copy To Local")
        self.keep_button = QPushButton("Refresh Lists")
        self.delete_button = QPushButton("Delete Remote VM")
        self.replace_button = QPushButton("Copy VM To Remote")
        self.refresh_button = QPushButton("Refresh VM Lists")

        self.copy_button.clicked.connect(self.copy_to_local)
        self.keep_button.clicked.connect(self.keep_local_copy)
        self.delete_button.clicked.connect(self.delete_selected_vm)
        self.replace_button.clicked.connect(self.copy_to_remote)
        self.refresh_button.clicked.connect(self.refresh_vm_lists)
        self.source_vm_list.itemSelectionChanged.connect(self._on_source_selection_changed)
        self.local_vm_list.itemSelectionChanged.connect(self._on_local_selection_changed)
        self.source_folder_input.editingFinished.connect(self._on_source_folder_changed)
        self.local_folder_input.editingFinished.connect(self._on_local_folder_changed)

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.addWidget(self._build_paths_group())
        layout.addWidget(self._build_vm_lists_group())
        layout.addWidget(self._build_actions_group())
        layout.addWidget(self._build_status_group())
        self.setCentralWidget(central)

        self._append_log("Select the remote folder and local folder, then choose a VM from the lists.")
        self._restore_settings()
        self._update_action_states()

    def _build_paths_group(self) -> QGroupBox:
        group = QGroupBox("Paths")
        layout = QGridLayout(group)

        source_button = QPushButton("Browse Remote Folder")
        source_button.clicked.connect(self.choose_source_folder)
        local_button = QPushButton("Browse Folder")
        local_button.clicked.connect(self.choose_local_folder)

        layout.addWidget(QLabel("Remote VM folder"), 0, 0)
        layout.addWidget(self.source_folder_input, 0, 1)
        layout.addWidget(source_button, 0, 2)
        layout.addWidget(QLabel("Local destination"), 1, 0)
        layout.addWidget(self.local_folder_input, 1, 1)
        layout.addWidget(local_button, 1, 2)
        layout.addWidget(self.refresh_button, 2, 2)
        return group

    def _build_vm_lists_group(self) -> QGroupBox:
        group = QGroupBox("VM Bundles")
        layout = QGridLayout(group)
        layout.addWidget(self.source_list_label, 0, 0)
        layout.addWidget(self.local_list_label, 0, 1)
        layout.addWidget(self.source_vm_list, 1, 0)
        layout.addWidget(self.local_vm_list, 1, 1)
        return group

    def _build_actions_group(self) -> QGroupBox:
        group = QGroupBox("Actions")
        layout = QHBoxLayout(group)
        layout.addWidget(self.copy_button)
        layout.addWidget(self.delete_button)
        layout.addWidget(self.replace_button)
        layout.addWidget(self.keep_button)
        return group

    def _build_status_group(self) -> QGroupBox:
        group = QGroupBox("Status")
        layout = QVBoxLayout(group)
        layout.addWidget(self.progress_bar)
        layout.addWidget(self.status_label)
        layout.addWidget(self.log_output)
        return group

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

    def refresh_vm_lists(self) -> None:
        source_folder = self._folder_path(self.source_folder_input)
        local_folder = self._folder_path(self.local_folder_input)
        self._set_list_labels(source_folder, local_folder)
        self._populate_vm_list(
            self.source_vm_list,
            source_folder,
            self.settings.get("source_vm_name", ""),
        )
        self._populate_vm_list(
            self.local_vm_list,
            local_folder,
            self.settings.get("local_vm_name", ""),
        )
        self._update_action_states()

    def _set_list_labels(self, source_folder: Path | None, local_folder: Path | None) -> None:
        source_text = str(source_folder) if source_folder is not None else "No folder selected"
        local_text = str(local_folder) if local_folder is not None else "No folder selected"
        self.source_list_label.setText(f"Remote folder VMs: {source_text}")
        self.local_list_label.setText(f"Local folder VMs: {local_text}")

    def _populate_vm_list(self, widget: QListWidget, folder: Path | None, preferred_name: str = "") -> None:
        current_name = preferred_name or self._selected_vm_name(widget)
        widget.clear()
        if folder is None:
            return
        try:
            bundles = list_vm_bundles(folder)
        except Exception as exc:  # noqa: BLE001
            self._append_log(f"Unable to scan {folder}: {exc}")
            return

        for bundle in bundles:
            item = QListWidgetItem(bundle.name)
            item.setData(Qt.ItemDataRole.UserRole, str(bundle))
            widget.addItem(item)
            if bundle.name == current_name:
                widget.setCurrentItem(item)

    def _folder_path(self, field: QLineEdit) -> Path | None:
        text = field.text().strip()
        return Path(text).expanduser() if text else None

    def _selected_vm_name(self, widget: QListWidget) -> str | None:
        selected_items = widget.selectedItems()
        item = selected_items[0] if selected_items else None
        return item.text() if item is not None else None

    def _selected_vm_path(self, widget: QListWidget) -> Path | None:
        selected_items = widget.selectedItems()
        item = selected_items[0] if selected_items else None
        if item is None:
            return None
        return Path(item.data(Qt.ItemDataRole.UserRole))

    def current_selection(self) -> VmSelection:
        local_parent = Path(self.local_folder_input.text()).expanduser()
        source_vm = self._selected_vm_path(self.source_vm_list)

        if not self.source_folder_input.text().strip():
            raise ValueError("Choose a remote folder first.")
        if not self.local_folder_input.text().strip():
            raise ValueError("Choose a local destination folder first.")
        if source_vm is None:
            raise ValueError("Choose a VM from the remote folder list first.")
        if not local_parent.exists():
            raise FileNotFoundError(f"Local destination folder does not exist: {local_parent}")

        ensure_pvm(source_vm)
        return VmSelection(source_vm=source_vm, local_parent=local_parent)

    def copy_to_local(self) -> None:
        selection = self._guard_selection()
        if selection is None:
            return

        local_vm = selection.local_vm
        if not can_write_to_folder(selection.local_parent):
            self._show_error(f"No write permission for local destination: {selection.local_parent}")
            return

        vm_size = compute_total_size(selection.source_vm)
        free_space = available_bytes(selection.local_parent)
        if vm_size > free_space:
            self._show_error("Not enough free space on the local drive for this VM.")
            return

        overwrite = False
        action_text = f"Copy {selection.source_vm.name} from remote to local folder."
        if local_vm.exists():
            confirmed = QMessageBox.question(
                self,
                "Overwrite Local VM",
                f"Overwrite the existing local VM?\n\n{local_vm}",
            )
            if confirmed != QMessageBox.StandardButton.Yes:
                return
            overwrite = True
            action_text = f"Overwrite local VM {local_vm.name} with the remote copy."

        self._append_log(f"About to: {action_text}")

        self._run_action(
            PendingAction(
                label="Copy to local completed",
                runner=lambda: copy_tree_with_progress(
                    selection.source_vm,
                    local_vm,
                    self._progress_callback,
                    overwrite=overwrite,
                ),
            )
        )

    def keep_local_copy(self) -> None:
        self._append_log("About to: refresh both VM lists.")
        self.refresh_vm_lists()
        self.progress_bar.setValue(100)
        self.status_label.setText("VM lists refreshed.")
        self._append_log("VM lists refreshed.")

    def delete_selected_vm(self) -> None:
        remote_vm = self._selected_vm_path(self.source_vm_list)
        local_vm = self._selected_vm_path(self.local_vm_list)
        source_folder = self._folder_path(self.source_folder_input)
        local_folder = self._folder_path(self.local_folder_input)
        if remote_vm is not None:
            if source_folder is None or not remote_vm.exists():
                self._show_error("There is no remote VM to delete.")
                return
            if not can_write_to_folder(source_folder):
                self._show_error(f"No write permission for remote destination: {source_folder}")
                return

            confirmed = QMessageBox.question(
                self,
                "Delete Remote VM",
                f"Delete remote VM?\n\n{remote_vm}",
            )
            if confirmed != QMessageBox.StandardButton.Yes:
                return

            self._append_log(f"About to: delete remote VM {remote_vm.name}.")
            self._run_action(
                PendingAction(
                    label="Delete remote VM completed",
                    runner=lambda: remove_tree(remote_vm),
                )
            )
            return

        if local_vm is None or local_folder is None or not local_vm.exists():
            self._show_error("There is no local VM copy to delete.")
            return
        if not can_write_to_folder(local_folder):
            self._show_error(f"No write permission for local destination: {local_folder}")
            return

        confirmed = QMessageBox.question(
            self,
            "Delete Local Copy",
            f"Delete local VM copy?\n\n{local_vm}",
        )
        if confirmed != QMessageBox.StandardButton.Yes:
            return

        self._append_log(f"About to: delete local VM {local_vm.name}.")

        self._run_action(
            PendingAction(
                label="Delete local copy completed",
                runner=lambda: remove_tree(local_vm),
            )
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

        vm_size = compute_total_size(local_vm)
        free_space = available_bytes(source_folder)
        if destination_vm.exists():
            free_space += compute_total_size(destination_vm)
        if vm_size > free_space:
            self._show_error("Not enough free space on the remote drive to replace the VM.")
            return

        overwrite = False
        action_text = f"Copy local VM {local_vm.name} to remote folder."
        if destination_vm.exists():
            confirmed = QMessageBox.question(
                self,
                "Overwrite Remote VM",
                f"Overwrite the existing remote VM?\n\n{destination_vm}",
            )
            if confirmed != QMessageBox.StandardButton.Yes:
                return
            overwrite = True
            action_text = f"Overwrite remote VM {destination_vm.name} with the local copy."

        self._append_log(f"About to: {action_text}")

        self._run_action(
            PendingAction(
                label="Copy VM to remote completed",
                runner=lambda: copy_tree_with_progress(
                    local_vm,
                    destination_vm,
                    self._progress_callback,
                    overwrite=overwrite,
                ),
            )
        )

    def _guard_selection(self) -> VmSelection | None:
        try:
            return self.current_selection()
        except Exception as exc:  # noqa: BLE001
            self._show_error(str(exc))
            return None

    def _run_action(self, action: PendingAction) -> None:
        if self._thread is not None and self._thread.isRunning():
            self._show_error("Another action is already running.")
            return

        self.progress_bar.setValue(0)
        self.status_label.setText("Working...")
        self._append_log(action.label.replace(" completed", " started"))

        self._thread = QThread(self)
        self._worker = Worker(action)
        self._worker.moveToThread(self._thread)

        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished.connect(self._on_finished)
        self._worker.failed.connect(self._on_failed)
        self._worker.finished.connect(self._thread.quit)
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

    def _on_failed(self, message: str) -> None:
        self.status_label.setText("Action failed")
        self._append_log(f"Error: {message}")
        self._show_error(message)

    def _cleanup_thread(self) -> None:
        if self._worker is not None:
            self._worker.deleteLater()
        if self._thread is not None:
            self._thread.deleteLater()
        self._worker = None
        self._thread = None

    def _show_error(self, message: str) -> None:
        QMessageBox.critical(self, "VMHandy", message)

    def _append_log(self, message: str) -> None:
        self.log_output.appendPlainText(message)

    def _update_action_states(self) -> None:
        source_folder = self._folder_path(self.source_folder_input)
        local_folder = self._folder_path(self.local_folder_input)
        source_vm = self._selected_vm_path(self.source_vm_list)
        local_vm = self._selected_vm_path(self.local_vm_list)
        source_selected = bool(self.source_vm_list.selectedItems())
        local_selected = bool(self.local_vm_list.selectedItems())

        copy_enabled = (
            source_selected
            and source_folder is not None
            and local_folder is not None
            and source_vm is not None
            and local_folder.exists()
            and can_write_to_folder(local_folder)
        )
        keep_enabled = True
        delete_enabled = (
            (source_selected and source_folder is not None and can_write_to_folder(source_folder))
            or (local_selected and local_folder is not None and can_write_to_folder(local_folder))
        )
        replace_enabled = (
            local_selected
            and local_vm is not None
            and source_folder is not None
            and source_folder.exists()
            and can_write_to_folder(source_folder)
        )

        self.copy_button.setEnabled(copy_enabled)
        self.keep_button.setEnabled(keep_enabled)
        self.delete_button.setEnabled(delete_enabled)
        self.replace_button.setEnabled(replace_enabled)
        if source_selected:
            self.delete_button.setText("Delete Remote VM")
        elif local_selected:
            self.delete_button.setText("Delete Local Copy")
        else:
            self.delete_button.setText("Delete")

    def _on_source_folder_changed(self) -> None:
        self.settings["source_folder"] = self.source_folder_input.text().strip()
        self.settings["source_vm_name"] = ""
        self._write_settings()
        self.refresh_vm_lists()

    def _on_local_folder_changed(self) -> None:
        self.settings["local_folder"] = self.local_folder_input.text().strip()
        self.settings["local_vm_name"] = ""
        self._write_settings()
        self.refresh_vm_lists()

    def _on_source_selection_changed(self) -> None:
        selected_name = self._selected_vm_name(self.source_vm_list) or ""
        if selected_name:
            self.local_vm_list.blockSignals(True)
            self.local_vm_list.clearSelection()
            self.local_vm_list.blockSignals(False)
            self.settings["local_vm_name"] = ""
        self.settings["source_vm_name"] = selected_name
        self._write_settings()
        self._update_action_states()

    def _on_local_selection_changed(self) -> None:
        selected_name = self._selected_vm_name(self.local_vm_list) or ""
        if selected_name:
            self.source_vm_list.blockSignals(True)
            self.source_vm_list.clearSelection()
            self.source_vm_list.blockSignals(False)
            self.settings["source_vm_name"] = ""
        self.settings["local_vm_name"] = selected_name
        self._write_settings()
        self._update_action_states()

    def _restore_settings(self) -> None:
        self.source_folder_input.setText(self.settings.get("source_folder", ""))
        self.local_folder_input.setText(self.settings.get("local_folder", ""))
        self.refresh_vm_lists()

    def closeEvent(self, event) -> None:  # noqa: N802
        self.settings["source_folder"] = self.source_folder_input.text().strip()
        self.settings["local_folder"] = self.local_folder_input.text().strip()
        self.settings["source_vm_name"] = self._selected_vm_name(self.source_vm_list) or ""
        self.settings["local_vm_name"] = self._selected_vm_name(self.local_vm_list) or ""
        self._write_settings()
        super().closeEvent(event)

    def _load_settings(self) -> dict[str, str]:
        settings = {
            "source_folder": "",
            "local_folder": "",
            "source_vm_name": "",
            "local_vm_name": "",
        }
        if not self.settings_path.exists():
            return settings

        for line in self.settings_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or line.startswith(";"):
                continue
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if key in settings:
                settings[key] = value.strip()
        return settings

    def _write_settings(self) -> None:
        lines = [
            "[vmhandy]",
            f"source_folder={self.settings.get('source_folder', '')}",
            f"local_folder={self.settings.get('local_folder', '')}",
            f"source_vm_name={self.settings.get('source_vm_name', '')}",
            f"local_vm_name={self.settings.get('local_vm_name', '')}",
            "",
        ]
        self.settings_path.write_text("\n".join(lines), encoding="utf-8")
