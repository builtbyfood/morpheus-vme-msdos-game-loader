# Phase 3 — DOS Games Installer

Installs **Wolfenstein 3D**, **Doom**, and **Duke Nukem 3D** shareware onto an existing MS-DOS 6.22 KVM VM managed by Morpheus + HPE VM Essentials.

Game files are downloaded directly on the VME host from [archive.org](https://archive.org) and written into the VM's qcow2 disk image while the VM is offline — no manual file copying required.

---

## Prerequisites

### 1. A working MS-DOS 6.22 VM

You need an existing KVM VM with MS-DOS 6.22 already installed and managed by Morpheus. See [Phase 1](../phase1-msdos622/) for automated installation.

The VM must have enough free disk space (~50 MB for all three games).

### 2. SSH key for the Morpheus task runner

Morpheus Python Script tasks run as the `morpheus-local` system user. This user needs passwordless SSH access to your VME host(s).

**Run once on your Morpheus appliance:**

```bash
# Generate a key pair for morpheus-local
sudo ssh-keygen -t ed25519 -f /opt/morpheus/.ssh/id_ed25519 -N ""
sudo chown -R morpheus-local:morpheus-local /opt/morpheus/.ssh
sudo chmod 700 /opt/morpheus/.ssh
sudo chmod 600 /opt/morpheus/.ssh/id_ed25519

# Print the public key
sudo cat /opt/morpheus/.ssh/id_ed25519.pub
```

**Authorize it on each VME host** (run as your VME SSH user):

```bash
sudo cat /opt/morpheus/.ssh/id_ed25519.pub | \
  ssh YOUR_VME_USER@YOUR_VME_HOST "mkdir -p ~/.ssh && cat >> ~/.ssh/authorized_keys && chmod 700 ~/.ssh && chmod 600 ~/.ssh/authorized_keys"

# Test it works:
sudo -u morpheus-local ssh -o BatchMode=yes YOUR_VME_USER@YOUR_VME_HOST "echo ok"
```

### 3. Passwordless sudo on the VME host

Add to `/etc/sudoers` on the VME host (use `visudo`):

```
YOUR_VME_USER ALL=(ALL) NOPASSWD: ALL
```

### 4. Packages on the VME host

```bash
sudo apt-get install -y qemu-utils wget unzip
```

---

## Morpheus Workflow Setup

### Task

| Field | Value |
|---|---|
| Name | `Install Retro DOS Games` |
| Type | Python Script |
| Result Type | Key/Value Pairs |
| Execute Target | Local |
| Script | *(paste contents of `install-dos-games.py`)* |

### Option List — "Migrate VM List"

Reuse from Phase 1/migration toolkit if available, or create:

| Field | Value |
|---|---|
| Name | `Migrate VM List` |
| Type | Morpheus API |
| Source URL | `/api/servers?max=500` |
| Real Time | ✓ |

Translation script (filters out hypervisor host nodes):

```javascript
results = [];
for (var x = 0; x < data.length; x++) {
  var s = data[x];
  if (s.computeServerType && s.computeServerType.vmHypervisor === false) {
    results.push({ name: s.name, value: s.id });
  }
}
```

### Inputs

| Label | Field Name | Type | Notes |
|---|---|---|---|
| Target VM | `migrate_vm_id` | Select (Option List) | Use "Migrate VM List" above |
| DOS VM Name (fallback) | `dos_vm_name` | Text | Plain text domain name if not using dropdown |
| VME SSH User | `vme_ssh_user` | Text | SSH username on VME host |
| VME Host (fallback) | `vme_host` | Text | IP/hostname if auto-detection fails |
| Credential ID | `credential_id` | Text | Morpheus Trust credential ID (optional) |
| Games to Install | `install_games` | Text | `all` or comma-separated: `wolf3d,doom,duke3d` |
| Update AUTOEXEC.BAT | `update_path` | Checkbox | Adds game dirs to PATH (recommended) |
| Start VM After | `start_vm_after` | Checkbox | Boot the VM when done |

### Workflow

| Field | Value |
|---|---|
| Name | `Install Retro DOS Games` |
| Type | Operational |
| Tasks | Add the task above |

---

## Running

1. Go to your DOS VM in Morpheus → **Actions → Workflow**
2. Select **Install Retro DOS Games**
3. Fill in: Target VM, VME SSH User — leave everything else as defaults
4. Click **Execute**

Expected runtime: ~3–5 minutes. The workflow will shut down the VM, download games (~40 MB) via wget on the VME host, copy files onto the disk, update AUTOEXEC.BAT, and optionally restart the VM.

---

## After Installation

Boot the VM and at the DOS prompt:

```
C:\> WOLF3D        ← Wolfenstein 3D
C:\> DOOM          ← Doom
C:\> DUKE3D        ← Duke Nukem 3D
```

---

## Game Sources

All downloaded from [archive.org](https://archive.org) as shareware:

| Game | Version | Key File |
|---|---|---|
| Wolfenstein 3D | v1.4 Shareware | `WOLF3D.EXE` |
| Doom | v1.9 Shareware | `DOOM1.WAD` |
| Duke Nukem 3D | v1.3D Shareware | `DUKE3D.GRP` |

Override any source with the `wolf3d_source`, `doom_source`, or `duke3d_source` inputs — accepts HTTP(S) URLs, `smb://` paths, or local paths on the VME host.

---

## Troubleshooting

**SSH fails** — Verify: `sudo -u morpheus-local ssh -o BatchMode=yes USER@HOST "echo ok"`

**qemu-nbd lock error** — Clear stale lock: `ssh USER@HOST "sudo qemu-nbd --disconnect /dev/nbd0"`

**Download fails** — VME host needs outbound internet access to archive.org. Use `*_source` inputs to point at a local copy instead.

**VM name shows as a number** — Expected. The script resolves it to the domain name via the Morpheus API automatically.
