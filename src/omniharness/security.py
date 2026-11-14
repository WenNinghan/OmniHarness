import hashlib
import os
import secrets
from datetime import timedelta, timezone

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import update

from .models import LocalSession, PairingCode, now
from .settings import Settings


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def init_secrets(settings: Settings) -> None:
    settings.secret_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = settings.secret_dir / "master.key"
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return
    with os.fdopen(fd, "wb") as stream:
        stream.write(Fernet.generate_key())


class SecretStore:
    def __init__(self, settings: Settings):
        path = settings.secret_dir / "master.key"
        if not path.is_file():
            raise RuntimeError("Master key is missing. Run 'omniharness bootstrap' first.")
        self._cipher = Fernet(path.read_bytes().strip())

    def encrypt(self, value: str | None) -> str | None:
        return self._cipher.encrypt(value.encode()).decode() if value else None

    def decrypt(self, value: str | None) -> str | None:
        if not value:
            return None
        try:
            return self._cipher.decrypt(value.encode()).decode()
        except InvalidToken:
            raise RuntimeError("Stored credential cannot be decrypted with this master key") from None


def future(value):
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value > now()


def create_pairing_code(factory) -> str:
    code = secrets.token_urlsafe(18)
    with factory.begin() as db:
        row = db.get(PairingCode, "local")
        if row is None:
            row = PairingCode(id="local")
            db.add(row)
        row.code_hash = digest(code)
        row.expires_at = now() + timedelta(minutes=15)
        row.consumed = False
    return code


def exchange_code(factory, code: str, session_days: int) -> str | None:
    with factory.begin() as db:
        claimed = db.execute(update(PairingCode).where(
            PairingCode.id == "local", PairingCode.consumed.is_(False),
            PairingCode.code_hash == digest(code), PairingCode.expires_at > now(),
        ).values(consumed=True))
        if claimed.rowcount != 1:
            return None
        token = secrets.token_urlsafe(32)
        db.add(LocalSession(token_hash=digest(token), expires_at=now() + timedelta(days=session_days)))
        return token


def valid_session(factory, token: str | None) -> bool:
    if not token:
        return False
    with factory() as db:
        row = db.get(LocalSession, digest(token))
        return bool(row and future(row.expires_at))
