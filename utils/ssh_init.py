import os

def init_ssh_files():
    """Ensure /root/.ssh/known_hosts exists for root-run bots."""
    os.makedirs("/root/.ssh", exist_ok=True)
    kh = "/root/.ssh/known_hosts"
    if not os.path.exists(kh):
        open(kh, "a").close()
    return kh
