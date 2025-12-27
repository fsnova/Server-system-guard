from utils.ssh_guard import ensure_ssh_ready
import os
import subprocess
import asyncssh
from crypto import dec


def _ensure_ssh_trust(host: str, port: int = 22) -> None:
    """Ensure host key exists in root known_hosts (non-interactive).

    This avoids 'Host key is not trusted' errors without requiring any manual SSH step.
    """
    ssh_dir = "/root/.ssh"
    known_hosts = os.path.join(ssh_dir, "known_hosts")
    os.makedirs(ssh_dir, exist_ok=True)

    # If already present, nothing to do
    try:
        check = subprocess.run(
            ["ssh-keygen", "-F", host, "-f", known_hosts],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if check.returncode == 0:
            return
    except FileNotFoundError:
        # OpenSSH tools not installed; fall back to disabling known_hosts in connect()
        return

    # Scan and append
    scan = subprocess.run(
        ["ssh-keyscan", "-p", str(port), "-H", host],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False,
    )
    if scan.stdout:
        with open(known_hosts, "a", encoding="utf-8") as f:
            f.write(scan.stdout)


def _repair_known_host(host: str, port: int = 22) -> None:
    """If the host key changed, remove old entry and re-scan."""
    ssh_dir = "/root/.ssh"
    known_hosts = os.path.join(ssh_dir, "known_hosts")
    os.makedirs(ssh_dir, exist_ok=True)

    try:
        subprocess.run(
            ["ssh-keygen", "-R", host, "-f", known_hosts],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except FileNotFoundError:
        return

    _ensure_ssh_trust(host, port)


async def reboot(server):
    host, port, user, pw = server

    ensure_ssh_ready(host, port)
    # Auto-trust the host key (no manual steps required)
    _ensure_ssh_trust(host, port)

    try:
        async with asyncssh.connect(
            host,
            port=port,
            username=user,
            password=dec(pw),
            known_hosts=None,
        ) as c:
            await c.run("reboot", check=False)
    except Exception as e:
        # If the key is missing/changed, repair and retry once
        msg = str(e).lower()
        if ("host key" in msg and ("not trusted" in msg or "not verifiable" in msg or "unknown" in msg)) or ("host key" in msg and "changed" in msg):
            _repair_known_host(host, port)
            async with asyncssh.connect(
                host,
                port=port,
                username=user,
                password=dec(pw),
                known_hosts=None,
            ) as c:
                await c.run("reboot", check=False)
        else:
            raise
