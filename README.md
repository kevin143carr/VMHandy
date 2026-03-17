# VMHandy

VMHandy is a desktop utility for moving a Parallels VM bundle (`.pvm`) from a USB drive to a local folder, then either keeping, deleting, or copying the updated VM back to the USB drive. It now includes a provider selector so you can distinguish between Parallels and VMware Fusion on macOS and view provider-managed VM inventory separately from folder contents.

## Planned workflow

1. Select the virtualization provider.
2. Select the VM bundle on the USB drive.
3. Select the local destination folder on the internal drive.
4. Copy the VM locally.
5. Use the registered VM list to register or unregister the local VM with the selected provider.
6. When finished with the VM, choose one of:
   - Keep the local copy
   - Delete the local copy
   - Replace the source VM on the USB drive with the local copy

## Provider support

- Parallels: folder copy plus `prlctl`-backed register, unregister, and registered VM discovery.
- VMware Fusion: provider selection and discovered VM listing from Fusion library paths. Register and unregister are not wired yet.

## Run locally

```bash
python3.12 -m pip install --user -e .
vmhandy
```

Or run it directly from the project folder:

```bash
python3.12 main.py
```
