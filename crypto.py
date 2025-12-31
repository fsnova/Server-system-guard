import os
import hashlib

sk = os.getenv("SECRET_KEY")
if not sk:
    raise RuntimeError("SECRET_KEY is not set. Please set it in .env or environment variables.")
k = hashlib.sha256(sk.encode()).digest()

import os,base64,hashlib
from cryptography.fernet import Fernet
k=hashlib.sha256(os.getenv("SECRET_KEY").encode()).digest()
f=Fernet(base64.urlsafe_b64encode(k))
def enc(t): return f.encrypt(t.encode()).decode()
def dec(t): return f.decrypt(t.encode()).decode()
