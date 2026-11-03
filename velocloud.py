# ============================================================
# 🚀 VELOCLOUD | QEMU/KVM DISCORD VPS BOT
# ============================================================

# ONLY THREE CONFIGURATION VALUES
BOT_TOKEN = "YOUR_DISCORD_BOT_TOKEN"
OWNER_ID = 123456789012345678
OWNER_USERNAME = "YOUR_DISCORD_USERNAME"


# ============================================================
# ⚙️ INTERNAL CONFIGURATION
# ============================================================

import os
import sys
import json
import time
import uuid
import shutil
import sqlite3
import asyncio
import secrets
import hashlib
import socket
import subprocess
import urllib.request

from pathlib import Path
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ui import View, Button, Select, Modal, TextInput


BOT_NAME = "VELOCLOUD"
PREFIX = "!"
BASE_DIR = Path("/var/lib/velocloud")
VM_DIR = BASE_DIR / "vms"
IMAGE_DIR = BASE_DIR / "images"
CLOUD_DIR = BASE_DIR / "cloud"
DB_FILE = BASE_DIR / "velocloud.db"

MAX_VPS_PER_USER = 1
MAX_RAM_MB = 4096
MAX_CPU = 2
DEFAULT_DISK_GB = 10

SSH_PORT_START = 2200
SSH_PORT_END = 2299

PUBLIC_IP = os.environ.get("PUBLIC_IP", "")

OS_IMAGES = {
    "Ubuntu 22.04": {
        "url": "https://cloud-images.ubuntu.com/jammy/current/jammy-server-cloudimg-amd64.img",
        "file": "ubuntu-22.04.qcow2"
    },
    "Ubuntu 24.04": {
        "url": "https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img",
        "file": "ubuntu-24.04.qcow2"
    },
    "Debian 12": {
        "url": "https://cloud.debian.org/images/cloud/bookworm/latest/debian-12-generic-amd64.qcow2",
        "file": "debian-12.qcow2"
    }
}

BASE_DIR.mkdir(parents=True, exist_ok=True)
VM_DIR.mkdir(parents=True, exist_ok=True)
IMAGE_DIR.mkdir(parents=True, exist_ok=True)
CLOUD_DIR.mkdir(parents=True, exist_ok=True)


# ============================================================
# 🎨 DISCORD DESIGN
# ============================================================

COLOR = 0x5865F2
GREEN = 0x2ECC71
RED = 0xE74C3C
ORANGE = 0xF39C12

intents = discord.Intents.default()

bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)


def embed(title, description="", color=COLOR):
    e = discord.Embed(
        title=f"🚀 {title}",
        description=description,
        color=color,
        timestamp=datetime.now(timezone.utc)
    )
    e.set_footer(text="VELOCLOUD • VPS Infrastructure")
    return e


def is_owner(user):
    return (
        user.id == OWNER_ID
        or user.name.lower() == OWNER_USERNAME.lower()
    )


def run_command(command, timeout=120):
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False
    )


def check_program(name):
    return shutil.which(name) is not None


def db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def initialize_database():
    with db() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS vps (
                id TEXT PRIMARY KEY,
                owner_id INTEGER NOT NULL,
                owner_name TEXT,
                name TEXT,
                os TEXT,
                ram INTEGER,
                cpu INTEGER,
                disk INTEGER,
                image TEXT,
                port INTEGER,
                ssh_key TEXT,
                status TEXT DEFAULT 'stopped',
                created TEXT,
                tunnel_pid INTEGER DEFAULT 0
            )
        """)

        con.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)


initialize_database()


# ============================================================
# 🔐 PERMISSIONS AND DATABASE
# ============================================================

def get_vps(vps_id):
    with db() as con:
        return con.execute(
            "SELECT * FROM vps WHERE id=?",
            (vps_id,)
        ).fetchone()


def get_user_vps(user_id):
    with db() as con:
        return con.execute(
            "SELECT * FROM vps WHERE owner_id=?",
            (user_id,)
        ).fetchall()


def get_all_vps():
    with db() as con:
        return con.execute(
            "SELECT * FROM vps ORDER BY created DESC"
        ).fetchall()


def can_manage(user, vm):
    return is_owner(user) or vm["owner_id"] == user.id


def save_setting(key, value):
    with db() as con:
        con.execute("""
            INSERT OR REPLACE INTO settings(key,value)
            VALUES(?,?)
        """, (key, str(value)))


def get_setting(key):
    with db() as con:
        row = con.execute(
            "SELECT value FROM settings WHERE key=?",
            (key,)
        ).fetchone()

    return row["value"] if row else None


# ============================================================
# 🖥️ HOST CHECKS
# ============================================================

def kvm_available():
    return Path("/dev/kvm").exists()


def host_public_ip():
    if PUBLIC_IP:
        return PUBLIC_IP

    try:
        with urllib.request.urlopen(
            "https://api.ipify.org",
            timeout=5
        ) as response:
            return response.read().decode().strip()
    except Exception:
        return "UNKNOWN"


def host_status():
    try:
        import psutil

        memory = psutil.virtual_memory()
        disk = psutil.disk_usage("/")

        return (
            f"🧠 RAM: {memory.used // 1024**2} / "
            f"{memory.total // 1024**2} MB\n"
            f"💾 Disk: {disk.percent}% used\n"
            f"⚡ CPU: {psutil.cpu_percent()}%"
        )
    except Exception:
        return "Host statistics unavailable."


def free_ssh_port():
    used = {
        vm["port"]
        for vm in get_all_vps()
        if vm["port"]
    }

    for port in range(SSH_PORT_START, SSH_PORT_END + 1):
        if port in used:
            continue

        try:
            with socket.socket() as s:
                s.bind(("0.0.0.0", port))
                return port
        except OSError:
            continue

    raise RuntimeError("No available SSH ports.")


# ============================================================
# 📦 DOWNLOAD OS IMAGES
# ============================================================

async def download_image(os_name):
    if os_name not in OS_IMAGES:
        raise ValueError("Unsupported operating system.")

    info = OS_IMAGES[os_name]
    destination = IMAGE_DIR / info["file"]

    if destination.exists() and destination.stat().st_size > 1000000:
        return destination

    await asyncio.to_thread(
        urllib.request.urlretrieve,
        info["url"],
        destination
    )

    if destination.stat().st_size < 1000000:
        destination.unlink(missing_ok=True)
        raise RuntimeError("OS image download failed.")

    return destination


# ============================================================
# 🔑 SSH KEY GENERATION
# ============================================================

def generate_ssh_key(directory):
    private_key = directory / "id_ed25519"

    result = run_command([
        "ssh-keygen",
        "-q",
        "-t", "ed25519",
        "-N", "",
        "-f", str(private_key)
    ])

    if result.returncode != 0:
        raise RuntimeError("SSH key generation failed.")

    return (
        private_key,
        Path(str(private_key) + ".pub").read_text().strip()
    )


# ============================================================
# ☁️ CLOUD INIT
# ============================================================

def create_cloud_init(directory, username, public_key):
    cloud_config = f"""#cloud-config
users:
  - name: {username}
    groups: [sudo]
    shell: /bin/bash
    sudo: ALL=(ALL) NOPASSWD:ALL
    lock_passwd: true
    ssh_authorized_keys:
      - {public_key}

ssh_pwauth: false
disable_root: true

package_update: true

packages:
  - openssh-server
  - curl
  - sudo

runcmd:
  - systemctl enable ssh
  - systemctl start ssh
"""

    config_file = directory / "user-data"
    config_file.write_text(cloud_config)

    meta_file = directory / "meta-data"
    meta_file.write_text(
        f"instance-id: {uuid.uuid4()}\n"
        f"local-hostname: velocloud-vps\n"
    )

    iso_file = directory / "cloud-init.iso"

    result = run_command([
        "cloud-localds",
        str(iso_file),
        str(config_file),
        str(meta_file)
    ])

    if result.returncode != 0:
        raise RuntimeError("Cloud-init ISO creation failed.")

    return iso_file


# ============================================================
# 🖥️ VM CREATION
# ============================================================

def create_vm_record(
    owner,
    name,
    os_name,
    ram,
    cpu,
    disk
):
    vm_id = uuid.uuid4().hex[:10]

    directory = VM_DIR / vm_id
    directory.mkdir(parents=True)

    image = IMAGE_DIR / OS_IMAGES[os_name]["file"]
    disk_file = directory / "disk.qcow2"

    result = run_command([
        "qemu-img",
        "create",
        "-f", "qcow2",
        "-F", "qcow2",
        "-b", str(image),
        str(disk_file),
        f"{disk}G"
    ])

    if result.returncode != 0:
        raise RuntimeError(result.stderr[:500])

    ssh_private, ssh_public = generate_ssh_key(directory)

    cloud_iso = create_cloud_init(
        directory,
        "velocloud",
        ssh_public
    )

    port = free_ssh_port()

    with db() as con:
        con.execute("""
            INSERT INTO vps (
                id, owner_id, owner_name, name, os,
                ram, cpu, disk, image, port, ssh_key,
                status, created
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            vm_id,
            owner.id,
            owner.name,
            name,
            os_name,
            ram,
            cpu,
            disk,
            str(disk_file),
            port,
            str(ssh_private),
            "stopped",
            datetime.now(timezone.utc).isoformat()
        ))

    return vm_id


# ============================================================
# ▶️ VM START / STOP / RESTART
# ============================================================

def vm_pid_file(vm_id):
    return VM_DIR / vm_id / "qemu.pid"


def vm_is_running(vm_id):
    pid_file = vm_pid_file(vm_id)

    if not pid_file.exists():
        return False

    try:
        pid = int(pid_file.read_text().strip())
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def start_vm(vm):
    vm_id = vm["id"]

    if vm_is_running(vm_id):
        return "VPS is already running."

    directory = VM_DIR / vm_id

    disk = Path(vm["image"])
    cloud_iso = directory / "cloud-init.iso"

    if not disk.exists() or not cloud_iso.exists():
        raise RuntimeError("VPS disk or cloud-init image missing.")

    command = [
        "qemu-system-x86_64",
        "-enable-kvm",
        "-daemonize",
        "-pidfile", str(vm_pid_file(vm_id)),
        "-name", vm["name"],
        "-machine", "q35",
        "-cpu", "host",
        "-smp", str(vm["cpu"]),
        "-m", str(vm["ram"]),
        "-drive", f"file={disk},format=qcow2,if=virtio",
        "-drive", f"file={cloud_iso},format=raw,media=cdrom,readonly=on",
        "-netdev",
        f"user,id=net0,hostfwd=tcp:0.0.0.0:{vm['port']}-:22",
        "-device", "virtio-net-pci,netdev=net0",
        "-display", "none",
        "-serial", "none",
        "-monitor", "none",
        "-no-reboot"
    ]

    result = run_command(command, timeout=60)

    if result.returncode != 0:
        raise RuntimeError(result.stderr[:700])

    with db() as con:
        con.execute(
            "UPDATE vps SET status='running' WHERE id=?",
            (vm_id,)
        )

    return "VPS started successfully."


def stop_vm(vm):
    vm_id = vm["id"]

    if not vm_is_running(vm_id):
        return "VPS is already stopped."

    pid_file = vm_pid_file(vm_id)
    pid = int(pid_file.read_text().strip())

    os.kill(pid, 15)

    for _ in range(15):
        if not vm_is_running(vm_id):
            break
        time.sleep(1)

    if vm_is_running(vm_id):
        os.kill(pid, 9)

    pid_file.unlink(missing_ok=True)

    with db() as con:
        con.execute(
            "UPDATE vps SET status='stopped' WHERE id=?",
            (vm_id,)
        )

    return "VPS stopped."


def restart_vm(vm):
    stop_vm(vm)
    time.sleep(2)
    return start_vm(vm)


# ============================================================
# 📊 VPS STATUS
# ============================================================

def get_vm_status(vm):
    if vm_is_running(vm["id"]):
        return "🟢 Running"

    return "🔴 Stopped"


def vps_embed(vm):
    ip = host_public_ip()

    e = embed(
        f"VPS: {vm['name']}",
        "━━━━━━━━━━━━━━━━━━━━",
        GREEN if vm_is_running(vm["id"]) else RED
    )

    e.add_field(name="🆔 VPS ID", value=vm["id"], inline=True)
    e.add_field(name="📡 Status", value=get_vm_status(vm), inline=True)
    e.add_field(name="💿 OS", value=vm["os"], inline=True)

    e.add_field(
        name="🧠 RAM",
        value=f"{vm['ram']} MB",
        inline=True
    )

    e.add_field(
        name="⚡ CPU",
        value=f"{vm['cpu']} Cores",
        inline=True
    )

    e.add_field(
        name="💾 Disk",
        value=f"{vm['disk']} GB",
        inline=True
    )

    e.add_field(
        name="🌐 Public IP",
        value=ip,
        inline=True
    )

    e.add_field(
        name="🔌 SSH Port",
        value=str(vm["port"]),
        inline=True
    )

    e.add_field(
        name="👤 Owner",
        value=f"<@{vm['owner_id']}>",
        inline=True
    )

    e.add_field(
        name="🔐 SSH Command",
        value=f"`ssh velocloud@{ip} -p {vm['port']}`",
        inline=False
    )

    return e


# ============================================================
# 🎛️ VPS MANAGEMENT PANEL
# ============================================================

class ConfirmDelete(View):
    def __init__(self, vm, user):
        super().__init__(timeout=60)
        self.vm = vm
        self.user = user

    @discord.ui.button(
        label="Confirm Delete",
        emoji="🗑️",
        style=discord.ButtonStyle.danger
    )
    async def confirm(self, interaction, button):
        if not can_manage(interaction.user, self.vm):
            return await interaction.response.send_message(
                "❌ Access denied.",
                ephemeral=True
            )

        try:
            stop_vm(self.vm)

            directory = VM_DIR / self.vm["id"]

            shutil.rmtree(directory, ignore_errors=True)

            with db() as con:
                con.execute(
                    "DELETE FROM vps WHERE id=?",
                    (self.vm["id"],)
                )

            await interaction.response.edit_message(
                content="🗑️ VPS deleted successfully.",
                embed=None,
                view=None
            )

        except Exception as exc:
            await interaction.response.send_message(
                f"❌ Delete failed: {exc}",
                ephemeral=True
            )

    @discord.ui.button(
        label="Cancel",
        emoji="↩️",
        style=discord.ButtonStyle.secondary
    )
    async def cancel(self, interaction, button):
        await interaction.response.edit_message(
            content="Deletion cancelled.",
            embed=None,
            view=None
        )


class ManagePanel(View):
    def __init__(self, vm):
        super().__init__(timeout=300)
        self.vm_id = vm["id"]

    async def get_vm(self, interaction):
        vm = get_vps(self.vm_id)

        if not vm or not can_manage(interaction.user, vm):
            await interaction.response.send_message(
                "❌ You cannot manage this VPS.",
                ephemeral=True
            )
            return None

        return vm

    @discord.ui.button(
        label="Start",
        emoji="▶️",
        style=discord.ButtonStyle.success,
        row=0
    )
    async def start(self, interaction, button):
        vm = await self.get_vm(interaction)
        if not vm:
            return

        await interaction.response.defer(ephemeral=True)

        try:
            message = await asyncio.to_thread(start_vm, vm)
            await interaction.followup.send(
                f"🟢 {message}",
                ephemeral=True
            )
        except Exception as exc:
            await interaction.followup.send(
                f"❌ {exc}",
                ephemeral=True
            )

    @discord.ui.button(
        label="Stop",
        emoji="⏹️",
        style=discord.ButtonStyle.danger,
        row=0
    )
    async def stop(self, interaction, button):
        vm = await self.get_vm(interaction)
        if not vm:
            return

        await interaction.response.defer(ephemeral=True)

        try:
            message = await asyncio.to_thread(stop_vm, vm)
            await interaction.followup.send(
                f"🔴 {message}",
                ephemeral=True
            )
        except Exception as exc:
            await interaction.followup.send(
                f"❌ {exc}",
                ephemeral=True
            )

    @discord.ui.button(
        label="Restart",
        emoji="🔄",
        style=discord.ButtonStyle.primary,
        row=0
    )
    async def restart(self, interaction, button):
        vm = await self.get_vm(interaction)
        if not vm:
            return

        await interaction.response.defer(ephemeral=True)

        try:
            message = await asyncio.to_thread(restart_vm, vm)
            await interaction.followup.send(
                f"🔄 {message}",
                ephemeral=True
            )
        except Exception as exc:
            await interaction.followup.send(
                f"❌ {exc}",
                ephemeral=True
            )

    @discord.ui.button(
        label="Refresh",
        emoji="♻️",
        style=discord.ButtonStyle.secondary,
        row=0
    )
    async def refresh(self, interaction, button):
        vm = await self.get_vm(interaction)
        if not vm:
            return

        await interaction.response.edit_message(
            embed=vps_embed(vm),
            view=self
        )

    @discord.ui.button(
        label="SSH Details",
        emoji="🔐",
        style=discord.ButtonStyle.primary,
        row=1
    )
    async def ssh(self, interaction, button):
        vm = await self.get_vm(interaction)
        if not vm:
            return

        ip = host_public_ip()

        await interaction.response.send_message(
            embed=embed(
                "🔐 Private SSH Details",
                f"**Host:** `{ip}`\n"
                f"**Port:** `{vm['port']}`\n"
                f"**Username:** `velocloud`\n"
                f"**Command:**\n"
                f"`ssh -i velocloud-key velocloud@{ip} -p {vm['port']}`\n\n"
                f"Download your private key from the VPS host:\n"
                f"`{vm['ssh_key']}`\n\n"
                "⚠️ Keep your private key secret."
            ),
            ephemeral=True
        )

    @discord.ui.button(
        label="SSHX Terminal",
        emoji="💻",
        style=discord.ButtonStyle.primary,
        row=1
    )
    async def sshx(self, interaction, button):
        vm = await self.get_vm(interaction)
        if not vm:
            return

        if not vm_is_running(vm["id"]):
            return await interaction.response.send_message(
                "❌ Start your VPS first.",
                ephemeral=True
            )

        await interaction.response.send_message(
            embed=embed(
                "💻 SSHX Terminal",
                "SSHX must be started from inside your VPS.\n\n"
                f"Connect first:\n"
                f"`ssh -i
