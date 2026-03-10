#!/usr/bin/env python3
"""
install-dos-games.py
Retro VM Lab — Phase 3: DOS Games Installer
Morpheus Operational Workflow Task

Installs Wolfenstein 3D, Doom, and/or Duke Nukem 3D shareware onto an
existing MS-DOS 6.22 KVM VM managed by Morpheus + HPE VM Essentials.
Game files are downloaded directly on the VME host from archive.org and
written into the VM's qcow2 disk image while the VM is offline.

Approach:
  1. Resolve VM name + disk path via Morpheus API and virsh dumpxml
  2. Shut down the VM cleanly (force after 45s timeout)
  3. Download selected game archives from archive.org via wget on the VME host
  4. Stage and extract archives in /tmp on the VME host
  5. Mount the VM's qcow2 disk offline via qemu-nbd
  6. Copy game files onto the DOS FAT16 partition (filenames uppercased)
  7. Optionally update AUTOEXEC.BAT to add game dirs to PATH
  8. Unmount, disconnect nbd, clean up staging
  9. Start VM if requested

ONE-TIME SETUP
  SSH key for the Morpheus task runner (morpheus-local) must be authorized
  on each VME host. See the repository README for setup instructions.

  Short version — on your Morpheus appliance:
    sudo ssh-keygen -t ed25519 -f /opt/morpheus/.ssh/id_ed25519 -N ""
    sudo chown -R morpheus-local /opt/morpheus/.ssh
    sudo cat /opt/morpheus/.ssh/id_ed25519.pub | \
      ssh YOUR_VME_USER@YOUR_VME_HOST "cat >> ~/.ssh/authorized_keys"

MORPHEUS TASK SETUP
  Type        : Python Script
  Result Type : Key/Value Pairs
  Execute As  : Local

MORPHEUS INPUTS
  migrate_vm_id   — VM to install games on (use "Migrate VM List" Option List
                    dropdown; value = server ID) OR use dos_vm_name below
  dos_vm_name     — libvirt domain name as plain text fallback if not using
                    the dropdown (e.g. ms-dos-6.22)
  vme_ssh_user    — SSH username on the VME host (e.g. travis)
  vme_host        — VME host IP/hostname fallback if parentServer lookup fails
  credential_id   — Morpheus Trust credential ID (SSH Key Pair type preferred;
                    username/password type falls back to the key file above)

OPTIONAL INPUTS
  install_games   — comma-separated list: wolf3d,doom,duke3d  or  all (default)
  game_source     — global source override (archive.org default)
  wolf3d_source   — per-game source override URL
  doom_source     — per-game source override URL
  duke3d_source   — per-game source override URL
  smb_user        — SMB username (if using smb:// source)
  smb_password    — SMB password (if using smb:// source)
  update_path     — add game dirs to AUTOEXEC.BAT PATH (default: true)
  start_vm_after  — start the VM when done (default: false)

REQUIREMENTS ON VME HOST
  - passwordless sudo for: virsh, qemu-nbd, modprobe, mount, umount, partprobe,
    mkdir, cp, find, mv, rm, wget, unzip, blkid, file
  - qemu-utils package (provides qemu-nbd)
  - wget and unzip

https://github.com/YOUR_USERNAME/retro-vm-lab
"""

import os
import sys
import time
import json
import tempfile
import subprocess
import urllib.request
import urllib.error
import re
import ssl
import stat

# ─────────────────────────────────────────────────────────────────────────────
# ANSI colours
# ─────────────────────────────────────────────────────────────────────────────
BOLD  = "\033[1m"
GREEN = "\033[92m"
CYAN  = "\033[96m"
YELLOW= "\033[93m"
RED   = "\033[91m"
DIM   = "\033[2m"
NC    = "\033[0m"

def header(msg):
    print(f"\n{BOLD}{CYAN}{'═'*54}{NC}")
    print(f"{BOLD}{CYAN}  {msg}{NC}")
    print(f"{BOLD}{CYAN}{'═'*54}{NC}")

def log(msg):   print(f"  {DIM}[{timestamp()}]{NC} {msg}")
def ok(msg):    print(f"  {GREEN}✓{NC} {msg}")
def warn(msg):  print(f"  {YELLOW}⚠{NC}  {msg}", file=sys.stderr)
def die(msg):   print(f"\n  {RED}✗ FATAL:{NC} {msg}\n", file=sys.stderr); sys.exit(1)

def timestamp():
    import datetime
    return datetime.datetime.now().strftime("%H:%M:%S")

# ─────────────────────────────────────────────────────────────────────────────
# Game catalogue
# ─────────────────────────────────────────────────────────────────────────────
GAMES = {
    "wolf3d": {
        "label":      "Wolfenstein 3D v1.4 Shareware",
        "short":      "Wolf3D",
        "input_flag": "install_wolf3d",
        "url_input":  "wolf3d_url",
        # archive.org: identifier Wolfenstein3d, file Wolfenstein3dV14sw.ZIP
        "primary_url":  "https://archive.org/download/Wolfenstein3d/Wolfenstein3dV14sw.ZIP",
        "fallback_url": "https://archive.org/download/wolfenstein3dms-dos/Wolfenstein%203D%20%281992%29%20%28MS-DOS%29.zip",
        "dos_dir":    "WOLF3D",
        "key_file":   "WOLF3D.EXE",
        "launch_cmd": "WOLF3D",
        "min_bytes":  900_000,
    },
    "doom": {
        "label":      "Doom v1.9 Shareware",
        "short":      "Doom",
        "input_flag": "install_doom",
        "url_input":  "doom_url",
        # doom19s.zip contains a DOS self-extractor (DEICE.EXE) — unusable on Linux.
        # Use archives that contain DOOM1.WAD directly as a loose file or plain zip.
        "primary_url":  "https://archive.org/download/doom-shareware-episode-1/doom1.wad",
        "fallback_url": "https://archive.org/download/DoomsharewareEpisode/doom.ZIP",
        "dos_dir":    "DOOM",
        "key_file":   "DOOM1.WAD",
        "launch_cmd": "DOOM",
        "min_bytes":  1_500_000,
    },
    "duke3d": {
        "label":      "Duke Nukem 3D v1.3D Shareware",
        "short":      "Duke3D",
        "input_flag": "install_duke3d",
        "url_input":  "duke3d_url",
        # v1.3D shareware zip containing DUKE3D.GRP directly
        "primary_url":  "https://archive.org/download/Duke_Nukem_3D_v1.3D_1996_3D_Realms/Duke%20Nukem%203D%20v1.3D%20%281996%29%283D%20Realms%29.zip",
        "fallback_url": "https://archive.org/download/DUKE3D_DOS/DUKE3D.zip",
        "dos_dir":    "DUKE3D",
        "key_file":   "DUKE3D.GRP",
        "launch_cmd": "DUKE3D",
        "min_bytes":  4_000_000,
    },
}

# ─────────────────────────────────────────────────────────────────────────────
# Read Morpheus inputs
# ─────────────────────────────────────────────────────────────────────────────


def read_inputs():
    """
    Morpheus Python Script tasks inject a 'morpheus' context object as a
    built-in. Custom option field values are in morpheus['customOptions'].
    Morpheus API credentials are in morpheus['morpheus'].
    """
    def opt(key, default=""):
        try:
            val = morpheus["customOptions"].get(key)
            if val is None:
                val = morpheus["customOptions"].get(key.upper())
            return str(val).strip() if val is not None else default
        except Exception:
            return default

    def boolopt(key, default="true"):
        return opt(key, default).lower() in ("true", "1", "yes", "on")

    inputs = {
        # Required
        "vm_name":        opt("migrate_vm_id") or opt("dos_vm_name") or opt("vm_name"),
        "ssh_user":       opt("vme_ssh_user"),
        "vme_host":       opt("vme_host"),
        "credential_id":  opt("credential_id"),
        # Morpheus API — injected automatically
        # Use 127.0.0.1 — task runs locally on the appliance; hostname may not resolve
        # from the sandboxed workspace environment
        "morpheus_host":  "https://127.0.0.1",
        "morpheus_token": morpheus["morpheus"]["apiAccessToken"],           
        # Per-game booleans (all default true)
        "install_wolf3d": boolopt("install_wolf3d", "true"),
        "install_doom":   boolopt("install_doom",   "true"),
        "install_duke3d": boolopt("install_duke3d", "true"),
        # Optional URL overrides
        "wolf3d_url":     opt("wolf3d_url"),
        "doom_url":       opt("doom_url"),
        "duke3d_url":     opt("duke3d_url"),
        # Post-install
        "update_path":    boolopt("update_path", "true"),
        "start_vm_after": boolopt("start_vm_after", "false"),
    }

    # Apply URL overrides from inputs
    for game_key, game in GAMES.items():
        override = inputs.get(game["url_input"], "")
        if override:
            game["primary_url"] = override

    return inputs


# ─────────────────────────────────────────────────────────────────────────────
# Morpheus API helpers
# ─────────────────────────────────────────────────────────────────────────────
def morpheus_api(inputs, method, path, body=None):
    host  = inputs["morpheus_host"].rstrip("/")
    token = inputs["morpheus_token"]
    if not host or not token:
        return None
    url = f"https://{host}{path}".replace("https://https://", "https://")
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type",  "application/json")
    # Skip cert verification for localhost — cert is issued for the hostname
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(req, timeout=30, context=ctx) as r:
            return json.loads(r.read())
    except Exception as e:
        warn(f"Morpheus API {method} {path}: {e}")
        return None

def resolve_vm(inputs):
    """
    Find the Morpheus server record for the VM.
    If vm_name is a numeric ID (from the 'Migrate VM List' Option List dropdown),
    fetch /api/servers/{id} directly and resolve the real domain name.
    Returns (instance_id, host_ip) or (None, None).
    """
    vm_name = inputs["vm_name"]

    if vm_name and str(vm_name).strip().isdigit():
        # Numeric = Morpheus server ID from the Option List (value: s.id)
        server_id = str(vm_name).strip()
        log(f"migrate_vm_id is a server ID ({server_id}) — fetching /api/servers/{server_id}")
        data = morpheus_api(inputs, "GET", f"/api/servers/{server_id}")
        match = (data or {}).get("server")
        if not match:
            die(f"Server ID {server_id} not found via Morpheus API.\n"
                f"  API error likely means the appliance URL is unreachable from the task runner.\n"
                f"  Set the 'dos_vm_name' input to the exact libvirt domain name as a fallback\n"
                f"  (run: virsh list --all on {inputs.get('vme_host','the VME host')} to find it).")
        # Update vm_name to the real libvirt domain name
        inputs["vm_name"] = match.get("name", vm_name)
        log(f"Resolved server ID {server_id} → domain '{inputs['vm_name']}'")
    else:
        log(f"Querying Morpheus API for VM '{vm_name}' ...")
        data = morpheus_api(inputs, "GET", f"/api/servers?name={urllib.request.quote(vm_name)}&max=5")
        if not data:
            return None, None
        servers = data.get("servers", [])
        match = None
        for s in servers:
            if s.get("name", "").lower() == vm_name.lower() or \
               s.get("externalId", "").lower() == vm_name.lower():
                match = s
                break
        if not match and servers:
            match = servers[0]
            warn(f"Exact match not found — using closest result: {match.get('name')}")
        if not match:
            warn("VM not found in Morpheus API — will rely on virsh + vme_host")
            return None, None

    instance_id = None
    containers = match.get("containers", [])
    if containers:
        # /api/servers/{id} returns containers as int IDs; /api/servers?name= returns dicts
        first = containers[0]
        if isinstance(first, dict):
            instance_id = first.get("instance", {}).get("id")
        # int container IDs don't give us an instance_id directly — leave as None

    # parentServer gives us the VME host IP for unmanaged guest VMs
    parent = match.get("parentServer") or {}
    host_ip = parent.get("sshHost") or parent.get("internalIp") or parent.get("externalIp")

    ok(f"Resolved VM — server_id={match.get('id')}  domain='{inputs['vm_name']}'  host={host_ip}")

    return instance_id, host_ip

# ─────────────────────────────────────────────────────────────────────────────
# SSH helpers  (Trust credential pattern)
# ─────────────────────────────────────────────────────────────────────────────
_ssh_key_file = None
_ssh_host     = None
_ssh_user     = None

def setup_ssh(inputs, host_ip):
    global _ssh_key_file, _ssh_host, _ssh_user

    _ssh_host = host_ip
    _ssh_user = inputs["ssh_user"]

    # Try to fetch SSH key (and username if not provided) from Morpheus Trust credential
    cred_id = inputs.get("credential_id", "")
    if cred_id:
        log(f"Fetching Trust credential {cred_id} from Morpheus ...")
        cred_data = morpheus_api(inputs, "GET", f"/api/credentials/{cred_id}")
        if cred_data:
            cred = cred_data.get("credential", {}) or {}
            # Pull username from credential if vme_ssh_user input was left blank
            if not _ssh_user:
                cred_user = cred.get("username") or cred.get("user") or ""
                if cred_user:
                    _ssh_user = cred_user
                    inputs["ssh_user"] = cred_user
                    ok(f"SSH user resolved from credential: {cred_user}")
            private_key = cred.get("privateKey", "")
            if private_key:
                tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".pem", delete=False)
                tmp.write(private_key)
                tmp.close()
                os.chmod(tmp.name, stat.S_IRUSR | stat.S_IWUSR)
                _ssh_key_file = tmp.name
                ok(f"SSH key loaded from Trust credential {cred_id}")
            else:
                # Username/password credential type — Morpheus never returns
                # plaintext passwords via the API, so we fall back to the
                # morpheus-local SSH key generated during one-time setup.
                #
                # NOTE: The sandbox sets HOME incorrectly, so SSH's default
                # ~/.ssh lookup fails. We hardcode the known path instead.
                # If your appliance layout differs, use an SSH Key Pair
                # credential type in Morpheus Trust to avoid this entirely.
                hardcoded = "/opt/morpheus/.ssh/id_ed25519"
                _ssh_key_file = hardcoded
                ok(f"Using morpheus-local SSH key: {hardcoded}")
        else:
            warn("Could not fetch Trust credential — falling back to agent/default key")
    else:
        log("No credential_id set — using SSH agent or default key (~/.ssh/id_rsa)")

def ssh_cmd(remote_cmd, check=True, capture=False):
    """Run a command on the VME host over SSH."""
    args = ["ssh", "-o", "StrictHostKeyChecking=no",
                   "-o", "BatchMode=yes",
                   "-o", "ConnectTimeout=15"]
    if _ssh_key_file:
        args += ["-i", _ssh_key_file]
    args += [f"{_ssh_user}@{_ssh_host}", remote_cmd]

    result = subprocess.run(args,
                            capture_output=capture,
                            text=True,
                            timeout=120)
    if check and result.returncode != 0:
        stderr = result.stderr.strip() if capture else ""
        raise RuntimeError(f"SSH command failed (rc={result.returncode}): {remote_cmd}\n{stderr}")
    return result

def ssh(remote_cmd, capture=False):
    return ssh_cmd(remote_cmd, check=True, capture=capture)

def ssh_out(remote_cmd):
    return ssh_cmd(remote_cmd, check=True, capture=True).stdout.strip()

def scp_to_host(local_path, remote_path):
    args = ["scp", "-o", "StrictHostKeyChecking=no",
                   "-o", "BatchMode=yes",
                   "-o", "ConnectTimeout=15",
                   "-r"]
    if _ssh_key_file:
        args += ["-i", _ssh_key_file]
    args += [local_path, f"{_ssh_user}@{_ssh_host}:{remote_path}"]
    result = subprocess.run(args, capture_output=True, text=True, timeout=300)
    if result.returncode != 0:
        raise RuntimeError(f"scp failed: {result.stderr.strip()}")

def cleanup_ssh_key():
    if _ssh_key_file and os.path.exists(_ssh_key_file):
        os.unlink(_ssh_key_file)

# ─────────────────────────────────────────────────────────────────────────────
# VM control
# ─────────────────────────────────────────────────────────────────────────────
def get_vm_disk_path(domain):
    """Parse virsh dumpxml to find the qcow2 disk path."""
    xml = ssh_out(f"sudo virsh dumpxml '{domain}'")
    # Look for <source file='...'> inside a disk device (not cdrom/floppy)
    # Find all disk entries, skip floppy and cdrom
    disk_blocks = re.findall(
        r"<disk[^>]+device=['\"]disk['\"][^>]*>.*?</disk>",
        xml, re.DOTALL
    )
    for block in disk_blocks:
        m = re.search(r"<source\s+file=['\"]([^'\"]+)['\"]", block)
        if m:
            return m.group(1)
    # Fallback: any source file that ends with qcow2/img/raw
    m = re.search(r"<source\s+file=['\"]([^'\"]+\.(qcow2|img|raw))['\"]", xml)
    if m:
        return m.group(1)
    die(f"Could not locate disk image path in virsh dumpxml for domain '{domain}'")

def shutdown_vm(domain, timeout=45):
    """Gracefully shut down VM, with forced destroy after timeout."""
    state = ssh_out(f"sudo virsh domstate '{domain}'")
    if "shut off" in state.lower():
        ok(f"VM '{domain}' is already shut off")
        return

    log(f"Sending shutdown to '{domain}' ...")
    ssh(f"sudo virsh shutdown '{domain}'", capture=False)

    waited = 0
    while waited < timeout:
        time.sleep(5)
        waited += 5
        state = ssh_out(f"sudo virsh domstate '{domain}'")
        log(f"  VM state: {state} ({waited}s/{timeout}s)")
        if "shut off" in state.lower():
            ok(f"VM '{domain}' shut down cleanly")
            return

    warn(f"VM did not shut down after {timeout}s — forcing off ...")
    ssh(f"sudo virsh destroy '{domain}'")
    time.sleep(3)
    ok(f"VM '{domain}' forced off")

def start_vm(domain):
    ssh(f"sudo virsh start '{domain}'")
    ok(f"VM '{domain}' started")

# ─────────────────────────────────────────────────────────────────────────────
# Archive download
# ─────────────────────────────────────────────────────────────────────────────
def download_file(url, dest_path, label, min_bytes=0):
    log(f"  Downloading {label} ...")
    log(f"    {url}")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "retro-vm-lab/1.0"})
        with urllib.request.urlopen(req, timeout=120) as r, open(dest_path, "wb") as f:
            total = 0
            while True:
                chunk = r.read(65536)
                if not chunk:
                    break
                f.write(chunk)
                total += len(chunk)
        size_kb = os.path.getsize(dest_path) // 1024
        log(f"    → {size_kb} KB")
        if min_bytes and os.path.getsize(dest_path) < min_bytes:
            raise ValueError(f"Download too small ({os.path.getsize(dest_path)} bytes, expected ≥{min_bytes})")
        return True
    except Exception as e:
        warn(f"  Download failed: {e}")
        if os.path.exists(dest_path):
            os.unlink(dest_path)
        return False



def find_free_nbd():
    """Find a /dev/nbdN device that's not currently in use."""
    for n in range(0, 8):
        dev = f"/dev/nbd{n}"
        # Check if device exists
        check = ssh_cmd(f"test -b {dev}", check=False)
        if check.returncode != 0:
            continue
        # Check if it's connected (has partition entries or size > 0)
        size = ssh_out(f"sudo blockdev --getsize64 {dev} 2>/dev/null || echo 0")
        if size.strip() == "0":
            return dev
    die("No free /dev/nbdN device found (tried nbd0-nbd7). "
        "Ensure qemu-nbd is not already running against this disk.")

def mount_vm_disk(disk_path, pid):
    """Load nbd module, connect qcow2, mount FAT partition. Returns mount point."""
    global _nbd_device, _mount_point

    log("Loading nbd kernel module ...")
    ssh("sudo modprobe nbd max_part=8")

    log("Finding free nbd device ...")
    nbd = find_free_nbd()
    _nbd_device = nbd
    log(f"  Using {nbd}")

    log(f"Connecting {disk_path} → {nbd} ...")
    # Clean up any stale nbd connections to this disk from a previous failed run
    ssh_cmd(
        f"for d in /dev/nbd{{0..7}}; do "
        f"  if sudo qemu-nbd --list 2>/dev/null | grep -q '{disk_path}'; then "
        f"    sudo qemu-nbd --disconnect $d 2>/dev/null; sleep 1; "
        f"  fi; "
        f"done; true",
        check=False
    )
    ssh(f"sudo qemu-nbd --fork --connect={nbd} '{disk_path}'")
    time.sleep(3)  # give nbd a moment to settle after fork

    log("Running partprobe ...")
    ssh(f"sudo partprobe {nbd}")
    time.sleep(1)

    # Detect the partition — standard DOS install is p1
    part = f"{nbd}p1"
    check = ssh_cmd(f"test -b {part}", check=False)
    if check.returncode != 0:
        # Try without 'p' suffix (some nbd drivers)
        part = f"{nbd}1"
        check2 = ssh_cmd(f"test -b {part}", check=False)
        if check2.returncode != 0:
            ssh(f"sudo qemu-nbd --disconnect {nbd}")
            die(f"No partition found on {nbd} after connecting {disk_path}.\n"
                f"  Check that the VM's disk has a valid DOS partition table.")

    # Confirm it's FAT (not strictly required but good sanity check)
    fstype = ssh_out(f"sudo blkid -o value -s TYPE {part} 2>/dev/null || echo unknown")
    if fstype and fstype not in ("vfat", "fat16", "fat32", "unknown"):
        warn(f"Unexpected filesystem type '{fstype}' on {part} — attempting mount anyway")

    mnt = f"/mnt/retro-games-{pid}"
    ssh(f"sudo mkdir -p '{mnt}'")
    # Mount with iocharset and codepage for clean DOS 8.3 uppercase names
    ssh(f"sudo mount -t vfat -o rw,uid=0,gid=0,fmask=0000,dmask=0000,"
        f"iocharset=iso8859-1,codepage=437 {part} '{mnt}'")

    _mount_point = mnt
    ok(f"Disk mounted at {mnt} (partition {part}, type {fstype})")
    return mnt

def unmount_vm_disk():
    """Unmount partition and disconnect nbd. Safe to call multiple times."""
    global _mount_point, _nbd_device
    if _mount_point:
        log(f"Unmounting {_mount_point} ...")
        ssh_cmd(f"sudo umount '{_mount_point}'", check=False)
        ssh_cmd(f"sudo rmdir '{_mount_point}'", check=False)
        _mount_point = None
    if _nbd_device:
        log(f"Disconnecting {_nbd_device} ...")
        time.sleep(1)
        ssh_cmd(f"sudo qemu-nbd --disconnect '{_nbd_device}'", check=False)
        _nbd_device = None

def cleanup_staging():
    if _staging_dir:
        ssh_cmd(f"sudo rm -rf '{_staging_dir}'", check=False)

# ─────────────────────────────────────────────────────────────────────────────
# Free space check
# ─────────────────────────────────────────────────────────────────────────────
def check_free_space(mnt, needed_bytes):
    raw = ssh_out(f"df -k '{mnt}' | tail -1 | awk '{{print $4}}'")
    try:
        free_kb = int(raw.strip())
        free_bytes = free_kb * 1024
        needed_mb = needed_bytes / (1024 * 1024)
        free_mb   = free_bytes  / (1024 * 1024)
        log(f"  Disk free: {free_mb:.1f} MB  |  Games need: {needed_mb:.1f} MB")
        if free_bytes < needed_bytes + 512_000:   # 512 KB buffer
            die(f"Not enough free space on DOS partition.\n"
                f"  Free: {free_mb:.1f} MB  |  Required: {needed_mb:.1f} MB\n"
                f"  Resize the VM disk before running this task.")
        ok("Free space check passed")
    except ValueError:
        warn(f"Could not parse free space (df returned '{raw}') — skipping space check")

# ─────────────────────────────────────────────────────────────────────────────
# Install game files onto mounted partition
# ─────────────────────────────────────────────────────────────────────────────
def install_game_files(mnt, staging_game_dir, dos_dir):
    """
    Copy files from staging_game_dir into mnt/dos_dir.
    staging_game_dir is the directory ON THE VME HOST containing the game files.
    """
    target = f"{mnt}/{dos_dir}"
    # Check if already installed
    check = ssh_cmd(f"test -d '{target}'", check=False)
    if check.returncode == 0:
        warn(f"  {dos_dir} already exists on disk — overwriting")
        ssh(f"sudo rm -rf '{target}'")

    ssh(f"sudo mkdir -p '{target}'")
    # Copy all files from staging into target dir
    # Use cp -r with source/* to avoid nesting an extra directory level
    ssh(f"sudo cp -r '{staging_game_dir}'/. '{target}/'")
    # Convert filenames to uppercase (FAT16 stores uppercase; Linux may differ)
    _uppercase_filenames(target)
    ok(f"  Installed to {dos_dir}/")

def _uppercase_filenames(remote_dir):
    """Rename all files in remote_dir to uppercase (FAT16 convention)."""
    # Single-line command — avoids newline quoting issues over SSH.
    # || true: FAT treats foo and FOO as same file; mv returns rc=1, which is fine.
    ssh(f"find '{remote_dir}' -depth -name '*[a-z]*' | "
        f"while IFS= read -r f; do "
        f"dir=$(dirname \"$f\"); base=$(basename \"$f\"); "
        f"upper=$(echo \"$base\" | tr '[:lower:]' '[:upper:]'); "
        f"[ \"$base\" != \"$upper\" ] && sudo mv -f \"$f\" \"$dir/$upper\" 2>/dev/null; "
        f"done; true")

# ─────────────────────────────────────────────────────────────────────────────
# AUTOEXEC.BAT PATH update
# ─────────────────────────────────────────────────────────────────────────────
def update_autoexec(mnt, dos_dirs):
    """
    Read AUTOEXEC.BAT from the mounted partition, ensure C:\\GAME_DIR entries
    are present in the PATH= or SET PATH= line. Write it back.
    """
    # Case-insensitive search for AUTOEXEC.BAT
    autoexec_path = None
    ls_out = ssh_out(f"ls '{mnt}/' 2>/dev/null")
    for fname in ls_out.split():
        if fname.upper() == "AUTOEXEC.BAT":
            autoexec_path = f"{mnt}/{fname}"
            break

    if not autoexec_path:
        # Create a fresh one
        autoexec_path = f"{mnt}/AUTOEXEC.BAT"
        log("  AUTOEXEC.BAT not found — creating new one")
        ssh(f"sudo touch '{autoexec_path}'")

    # Read current contents
    current = ssh_out(f"sudo cat '{autoexec_path}' 2>/dev/null || echo ''")

    lines = current.splitlines()

    # Build the paths to add: C:\WOLF3D;C:\DOOM;C:\DUKE3D
    new_path_entries = [f"C:\\{d}" for d in dos_dirs]

    # Find existing PATH= line (case-insensitive)
    path_line_idx = None
    for i, line in enumerate(lines):
        if re.match(r'^\s*(SET\s+)?PATH\s*=', line, re.IGNORECASE):
            path_line_idx = i
            break

    if path_line_idx is not None:
        existing = lines[path_line_idx]
        # Add entries that aren't already present
        for entry in new_path_entries:
            if entry.upper() not in existing.upper():
                existing = existing.rstrip(";") + f";{entry}"
        lines[path_line_idx] = existing
        log(f"  Updated PATH line: {lines[path_line_idx]}")
    else:
        # Append a new PATH line after any existing @ECHO OFF / PROMPT lines
        insert_at = len(lines)
        for i, line in enumerate(lines):
            if re.match(r'^\s*(@ECHO|PROMPT|SET\s+PROMPT|VER\b)', line, re.IGNORECASE):
                insert_at = i + 1
        path_line = "SET PATH=C:\\;C:\\DOS;" + ";".join(new_path_entries)
        lines.insert(insert_at, path_line)
        log(f"  Added PATH line: {path_line}")

    new_content = "\r\n".join(lines)
    if not new_content.endswith("\r\n"):
        new_content += "\r\n"

    # Write back via heredoc-style echo (avoids quoting nightmares with special chars)
    tmp_remote = f"/tmp/autoexec_new_{os.getpid()}.bat"
    # Write locally first, then scp
    with tempfile.NamedTemporaryFile(mode="w", suffix=".bat", delete=False, newline="\r\n") as tf:
        tf.write(new_content)
        local_tmp = tf.name

    scp_to_host(local_tmp, tmp_remote)
    os.unlink(local_tmp)
    ssh(f"sudo cp '{tmp_remote}' '{autoexec_path}' && sudo rm -f '{tmp_remote}'")
    ok(f"  AUTOEXEC.BAT updated with game dirs in PATH")

# ─────────────────────────────────────────────────────────────────────────────
# Morpheus instance tagging
# ─────────────────────────────────────────────────────────────────────────────
def tag_instance(inputs, instance_id, installed_games):
    if not instance_id:
        return
    games_str = ",".join(g["short"].lower() for g in installed_games)
    payload = {
        "instance": {
            "tags": [
                {"name": "games",  "value": games_str},
                {"name": "series", "value": "retro-vm-lab"},
                {"name": "phase",  "value": "3"},
            ]
        }
    }
    result = morpheus_api(inputs, "PUT", f"/api/instances/{instance_id}", payload)
    if result:
        ok(f"Morpheus instance {instance_id} tagged (games={games_str})")
    else:
        warn("Morpheus tag update failed (non-fatal)")

# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────
def main():
    global _staging_dir

    print(f"\n{BOLD}{CYAN}╔══════════════════════════════════════════════════════╗{NC}")
    print(f"{BOLD}{CYAN}║   retro-vm-lab  Phase 3 — DOS Games Installer        ║{NC}")
    print(f"{BOLD}{CYAN}║   Doom  ·  Duke Nukem 3D  ·  Wolfenstein 3D          ║{NC}")
    print(f"{BOLD}{CYAN}╚══════════════════════════════════════════════════════╝{NC}\n")

    # ── Phase 1: Validate inputs ─────────────────────────────────────────────
    header("Phase 1: Validate Inputs")

    inputs = read_inputs()

    vm_name = inputs["vm_name"]
    if not vm_name:
        die("VM name is required. Supply either:\n"
            "  • migrate_vm_id — Morpheus 'Migrate VM List' Option List dropdown (server ID)\n"
            "  • dos_vm_name   — plain text input with the libvirt domain name")
    ok(f"VM name:    {vm_name}")

    if not inputs["ssh_user"] and not inputs.get("credential_id"):
        die("vme_ssh_user input is required (or set credential_id to resolve it automatically)")
    if inputs["ssh_user"]:
        ok(f"SSH user:   {inputs['ssh_user']} (from input)")
    else:
        ok(f"SSH user will be resolved from credential_id={inputs['credential_id']}")

    # Determine which games to install
    selected = {k: v for k, v in GAMES.items() if inputs.get(v["input_flag"], True)}
    if not selected:
        die("No games selected — enable at least one of: install_wolf3d, install_doom, install_duke3d")

    for k, g in selected.items():
        ok(f"Will install: {g['label']}")

    log(f"Update AUTOEXEC.BAT PATH: {inputs['update_path']}")
    log(f"Start VM after install:   {inputs['start_vm_after']}")

    pid = os.getpid()

    # ── Phase 2: Resolve VM ──────────────────────────────────────────────────
    header("Phase 2: Resolve VM")

    instance_id, host_ip_api = resolve_vm(inputs)
    vm_name = inputs["vm_name"]   # refresh — resolve_vm may have updated this from numeric ID

    # Determine host — API result takes priority, then the vme_host fallback input
    host_ip = host_ip_api or inputs.get("vme_host", "")
    if not host_ip:
        die("Could not resolve VME host IP.\n"
            "  Either the VM was not found in Morpheus, or the parentServer field is empty.\n"
            "  Set the 'vme_host' input as a fallback.")
    ok(f"VME host IP: {host_ip}")

    setup_ssh(inputs, host_ip)

    # Test SSH connectivity
    log("Testing SSH connectivity ...")
    test_out = ssh_out("echo pong")
    if "pong" not in test_out:
        die("SSH connectivity test failed")
    ok("SSH connectivity confirmed")


    # Verify domain exists
    log(f"Verifying libvirt domain '{vm_name}' ...")
    domain_check = ssh_out(f"sudo virsh list --all --name 2>/dev/null | grep -Fx '{vm_name}' || echo NOT_FOUND")
    if "NOT_FOUND" in domain_check or not domain_check.strip():
        die(f"Libvirt domain '{vm_name}' not found on host {host_ip}.\n"
            f"  Run: virsh list --all   to see available domains.")
    ok(f"Libvirt domain confirmed: {vm_name}")

    # Get disk path
    log("Resolving VM disk path from virsh dumpxml ...")
    disk_path = get_vm_disk_path(vm_name)
    ok(f"Disk image: {disk_path}")

    # Verify disk file exists
    disk_check = ssh_cmd(f"test -f '{disk_path}'", check=False)
    if disk_check.returncode != 0:
        die(f"Disk image not found: {disk_path}")

    # Detect disk format
    disk_fmt = ssh_out(f"sudo qemu-img info --output=json '{disk_path}' | python3 -c \"import json,sys; d=json.load(sys.stdin); print(d.get('format','unknown'))\" 2>/dev/null || echo unknown")
    ok(f"Disk format: {disk_fmt}")

    # ── Phase 3: Shut Down VM ────────────────────────────────────────────────
    header("Phase 3: Shut Down VM")
    shutdown_vm(vm_name)

    # ── Phase 4: Download & Stage Games ─────────────────────────────────────
    # Downloads happen on the VME host via wget — the Morpheus task runner
    # sandbox has no external DNS, but the VME host does.
    header("Phase 4: Download & Stage Games")

    staging = f"/tmp/dos-games-stage-{pid}"
    _staging_dir = staging
    ssh(f"sudo mkdir -p '{staging}'")

    # Ensure wget and unzip are available on VME host
    ssh("which wget >/dev/null 2>&1 || sudo apt-get install -y wget", )
    ssh("which unzip >/dev/null 2>&1 || sudo apt-get install -y unzip")

    game_source_dirs = {}
    total_bytes_needed = 0

    for game_key, game in selected.items():
        log(f"")
        log(f"── {game['label']} ──")
        remote_zip  = f"{staging}/{game_key}.zip"
        remote_stage = f"{staging}/{game_key}"
        ssh(f"sudo mkdir -p '{remote_stage}'")

        # Try primary URL, then fallback — wget on the VME host
        downloaded = False
        for label, url in (("primary", game["primary_url"]), ("fallback", game["fallback_url"])):
            log(f"  Downloading {game['short']} ({label}): {url}")
            result = ssh_cmd(
                f"sudo wget -q --show-progress -O '{remote_zip}' '{url}' 2>&1 | tail -3 && "
                f"test $(stat -c%s '{remote_zip}') -ge {game['min_bytes']}",
                check=False
            )
            if result.returncode == 0:
                ok(f"  Downloaded {game['short']} ({label})")
                downloaded = True
                break
            else:
                warn(f"  {label} download failed — trying next")
                ssh_cmd(f"sudo rm -f '{remote_zip}'", check=False)

        if not downloaded:
            die(f"Could not download {game['label']} on VME host.\n"
                f"  Check that {host_ip} has internet access and wget installed.\n"
                f"  Override with {game['url_input']} input to use a local/SMB path.")

        # Extract zip on VME host — or handle bare file (e.g. doom1.wad downloaded directly)
        log(f"  Extracting {game['short']} on VME host ...")
        key_file = game["key_file"]

        # Detect if downloaded file is a bare data file rather than a zip
        file_type = ssh_out(f"file -b '{remote_zip}' 2>/dev/null || echo unknown")
        if "zip" in file_type.lower() or "archive" in file_type.lower() or "compress" in file_type.lower():
            result = ssh_cmd(
                f"sudo unzip -o -q '{remote_zip}' -d '{remote_stage}' 2>&1 | head -5",
                check=False
            )
            if result.returncode not in (0, 1):
                die(f"Failed to extract {game['label']} on VME host (rc={result.returncode})")
            ssh_cmd(f"sudo rm -f '{remote_zip}'", check=False)
        else:
            # Bare file (WAD, GRP, EXE) — move into stage dir with correct name
            ssh(f"sudo mv '{remote_zip}' '{remote_stage}/{key_file.upper()}'")
            ok(f"  Bare file download — moved to {remote_stage}/{key_file.upper()}")

        # Find the directory containing the key file
        key_file = game["key_file"]
        found_dir = ssh_out(
            f"find '{remote_stage}' -iname '{key_file}' 2>/dev/null | head -1 | xargs -I{{}} dirname {{}}"
        )
        if not found_dir:
            # List what's there to help debug
            files = ssh_out(f"find '{remote_stage}' -type f 2>/dev/null | head -20")
            die(f"Key file '{key_file}' not found in {game['short']} archive.\n"
                f"  Files found: {files}\n"
                f"  Override with {game['url_input']} input.")

        ok(f"  Extracted {game['short']} — {key_file} found in {found_dir}")

        # Estimate size
        size_out = ssh_out(f"du -sb '{found_dir}' 2>/dev/null | cut -f1 || echo 0")
        try:
            total_bytes_needed += int(size_out.strip())
        except ValueError:
            total_bytes_needed += game["min_bytes"]

        game_source_dirs[game_key] = found_dir

    ok(f"All selected games downloaded and staged ({total_bytes_needed // (1024*1024)} MB total)")

    # ── Phase 5: Mount DOS Disk ──────────────────────────────────────────────
    header("Phase 5: Mount DOS Disk")

    # Ensure qemu-nbd is available
    nbd_check = ssh_cmd("which qemu-nbd 2>/dev/null || sudo apt-get install -y qemu-utils 2>&1 | tail -3", check=False)
    if nbd_check.returncode != 0:
        warn("qemu-nbd may not be installed — attempting install ...")
        ssh("sudo apt-get install -y qemu-utils")

    mnt = mount_vm_disk(disk_path, pid)

    # ── Free space check ──────────────────────────────────────────────────────
    check_free_space(mnt, total_bytes_needed)

    # ── Phase 6: Install Games ───────────────────────────────────────────────
    header("Phase 6: Install Games")

    installed_games = []
    installed_dirs  = []

    for game_key, game in selected.items():
        log(f"Installing {game['label']} → {mnt}/{game['dos_dir']} ...")
        install_game_files(mnt, game_source_dirs[game_key], game["dos_dir"])
        # Verify key file made it
        key_on_disk = ssh_out(
            f"find '{mnt}/{game['dos_dir']}' -maxdepth 1 -iname '{game['key_file']}' 2>/dev/null | head -1"
        )
        if not key_on_disk:
            warn(f"  Key file '{game['key_file']}' not found at {mnt}/{game['dos_dir']} — install may be incomplete")
        else:
            ok(f"  {game['short']}: {game['key_file']} verified ✓")
        installed_games.append(game)
        installed_dirs.append(game["dos_dir"])

    # ── Phase 7: Update AUTOEXEC.BAT ─────────────────────────────────────────
    if inputs["update_path"] and installed_dirs:
        header("Phase 7: Update AUTOEXEC.BAT")
        update_autoexec(mnt, installed_dirs)

    # ── Phase 8: Unmount & Cleanup ────────────────────────────────────────────
    header("Phase 8: Unmount & Cleanup")

    unmount_vm_disk()
    ok("Disk unmounted and nbd disconnected")

    cleanup_staging()
    ok("VME host staging files removed")

    ok("Local temp files removed (downloads ran on VME host)")

    cleanup_ssh_key()

    # ── Phase 9: Tag & Report ─────────────────────────────────────────────────
    header("Phase 9: Tag & Report")

    tag_instance(inputs, instance_id, installed_games)

    if inputs["start_vm_after"]:
        log(f"Starting VM '{vm_name}' ...")
        start_vm(vm_name)
    else:
        log(f"VM '{vm_name}' remains shut off (start_vm_after=false)")

    # ── Done! ─────────────────────────────────────────────────────────────────
    header("Done!")
    print()
    print(f"  {BOLD}{GREEN}Retro games installed on: {vm_name}{NC}")
    print()
    print(f"  {'Game':<22} {'Directory':<12} Launch command")
    print(f"  {'─'*22} {'─'*12} {'─'*14}")
    for game in installed_games:
        print(f"  {game['label']:<22} C:\\{game['dos_dir']:<11} {game['launch_cmd']}")
    print()
    if inputs["update_path"]:
        print(f"  AUTOEXEC.BAT: Game dirs added to PATH")
        print(f"  You can launch any game from any directory at the C:\\> prompt.")
    else:
        print(f"  To launch, type:  CD \\DOOM   then   DOOM")
    print()
    print(f"  Disk:     {disk_path}")
    print(f"  Format:   {disk_fmt}")
    print()
    print(f"  Next: Open VM console in Morpheus, power on, enjoy!")
    print()

    # Morpheus result output
    result_games = ",".join(g["short"] for g in installed_games)
    print(f"STATUS=success")
    print(f"GAMES_INSTALLED={result_games}")
    print(f"VM_NAME={vm_name}")


if __name__ == "__main__":
    # Register cleanup on unexpected exit
    import atexit
    atexit.register(unmount_vm_disk)
    atexit.register(cleanup_staging)
    atexit.register(cleanup_ssh_key)
    main()
