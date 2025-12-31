import subprocess
from .ssh_init import init_ssh_files

def ensure_ssh_ready(host: str, port: int = 22):
    known_hosts = init_ssh_files()

    try:
        check = subprocess.run(
            ["ssh-keygen", "-F", host, "-f", known_hosts],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if check.returncode == 0:
            return

        subprocess.run(
            ["ssh-keygen", "-R", host, "-f", known_hosts],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )

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
    except FileNotFoundError:
        return
