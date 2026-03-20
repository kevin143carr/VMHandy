# VMHandy

VMHandy is a macOS desktop utility for managing virtual machine bundles between a removable or remote folder and a local working folder. It provides a PySide6 GUI for copying, deleting, registering, and unregistering Parallels and VMware Fusion VMs.

## What It Does

- Select a virtualization provider: Parallels or VMware Fusion.
- Browse a remote/source folder and a local destination folder.
- List VM bundles in each folder with bundle size.
- Copy a VM from the remote folder to the local folder.
- Copy a local VM back to the remote folder, with overwrite confirmation.
- Delete a selected remote VM or local VM bundle.
- Show the selected provider's registered VM inventory.
- Register a local VM with the selected provider.
- Unregister a registered VM from the selected provider.
- Cancel an in-progress copy operation.
- Persist the selected provider, folders, and current selections using Qt user settings on macOS.

## Provider Support

### Parallels

- Detects `.pvm` bundles in the selected folders.
- Uses `prlctl` to list registered VMs.
- Registers VMs with `prlctl register --preserve-uuid`.
- Unregisters VMs with `prlctl unregister`.

### VMware Fusion

- Detects `.vmwarevm` bundles in the selected folders.
- Reads registered VMs from `~/Library/Application Support/VMware Fusion/vmInventory`.
- Registers and unregisters VMs by updating the Fusion inventory file directly.
- Creates a timestamped backup of the inventory file before rewriting it.
- May require restarting VMware Fusion if its library does not refresh after a register or unregister action.

## Workflow

1. Launch the app.
2. Choose the provider.
3. Choose the remote VM folder and local destination folder.
4. Select a VM in the remote list and use `Copy To Local` to bring it onto local storage.
5. Optionally register the local VM with the selected provider.
6. When finished, either keep the local copy, delete it, or use `Copy VM To Remote` to push it back to the source folder.

## Safety Checks And Behavior

- Requires Python 3.12 or newer.
- Verifies the selected bundle matches the active provider suffix.
- Refuses copy and delete actions when the destination folder is not writable.
- Checks free space before copying.
- Confirms overwrite before replacing an existing VM bundle.
- During overwrite, temporarily renames the old destination and restores it if the copy fails.
- Marks copied VM bundles as macOS bundles so Finder treats them correctly.
- If you delete a local VM that is still registered, VMHandy unregisters it first when the provider supports that operation.

## Requirements

- macOS
- Python 3.12+
- PySide6
- For Parallels integration: `prlctl` available on `PATH`
- For VMware Fusion integration: a readable `vmInventory` file, and a writable one for register/unregister actions

## Run Locally

Install in editable mode:

```bash
python3.12 -m pip install --user -e .
vmhandy
```

Or run directly from the project folder:

```bash
python3.12 main.py
```

## Project Files

- `main.py`: application entrypoint and Python version guard
- `ui.py`: PySide6 window, actions, settings, and status UI
- `file_ops.py`: VM bundle discovery, copy/delete helpers, and provider integrations
