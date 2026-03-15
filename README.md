# VMHandy

VMHandy is a desktop utility for moving a Parallels VM bundle (`.pvm`) from a USB drive to a local folder, then either keeping, deleting, or copying the updated VM back to the USB drive.

## Planned workflow

1. Select the VM bundle on the USB drive.
2. Select the local destination folder on the internal drive.
3. Copy the VM locally.
4. When finished with the VM, choose one of:
   - Keep the local copy
   - Delete the local copy
   - Replace the source VM on the USB drive with the local copy

## Run locally

```bash
python3.12 -m pip install --user -e .
vmhandy
```

Or run it directly from the project folder:

```bash
python3.12 main.py
```
