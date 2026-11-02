import hashlib
import secrets
from pathlib import Path

from cryptography.fernet import Fernet


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


class Vault:
    def __init__(self, key_file):
        self.cipher = Fernet(Path(key_file).read_bytes().strip())

    def encrypt(self, value):
        return self.cipher.encrypt(value.encode()).decode()

    def decrypt(self, value):
        return self.cipher.decrypt(value.encode()).decode()


def create_key(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as f:
        f.write(Fernet.generate_key())
    path.chmod(0o600)


def new_session():
    return secrets.token_urlsafe(48), secrets.token_urlsafe(32)
