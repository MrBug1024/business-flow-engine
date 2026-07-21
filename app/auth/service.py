"""Account registration and session lifecycle."""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets

from app.core.config import settings

from .mailer import VerificationMailer
from .models import Account
from .store import AccountStore


EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
SCRYPT_N = 1 << 14
SCRYPT_R = 8
SCRYPT_P = 1


class AuthenticationError(ValueError):
    pass


class InvalidEmailError(ValueError):
    pass


class InvalidPasswordError(ValueError):
    pass


class AuthService:
    def __init__(
        self,
        store: AccountStore,
        mailer: VerificationMailer,
        *,
        secret: str,
    ) -> None:
        self.store = store
        self.mailer = mailer
        self.secret = secret.encode("utf-8")

    def request_registration_code(self, email: str) -> int:
        normalized = normalize_email(email)
        code = f"{secrets.randbelow(1_000_000):06d}"
        code_id = self.store.create_verification_code(
            normalized,
            self._verification_hash(normalized, code),
            ttl_seconds=max(60, settings.verification_code_ttl_minutes * 60),
            resend_seconds=max(1, settings.verification_code_resend_seconds),
            max_per_hour=max(1, settings.verification_code_max_per_hour),
        )
        try:
            self.mailer.send_registration_code(normalized, code)
        except Exception:
            self.store.invalidate_verification_code(code_id)
            raise
        return max(1, settings.verification_code_resend_seconds)

    def register(self, email: str, password: str, code: str) -> tuple[Account, bool]:
        normalized = normalize_email(email)
        validate_password(password)
        normalized_code = code.strip()
        if len(normalized_code) != 6 or not normalized_code.isdigit():
            raise AuthenticationError("验证码格式不正确。")
        return self.store.register_account(
            normalized,
            hash_password(password),
            self._verification_hash(normalized, normalized_code),
            max_attempts=max(1, settings.verification_code_max_attempts),
        )

    def login(self, email: str, password: str) -> Account:
        normalized = normalize_email(email)
        found = self.store.account_by_email(normalized)
        password_hash = found[1] if found is not None else DUMMY_PASSWORD_HASH
        password_valid = verify_password(password, password_hash)
        if found is None or not password_valid:
            raise AuthenticationError("邮箱或密码不正确。")
        return found[0]

    def create_session(self, account: Account) -> tuple[str, float]:
        token = secrets.token_urlsafe(48)
        expires_at = self.store.create_session(
            account.id,
            session_token_hash(token),
            max(3600, settings.jwt_expire_hours * 3600),
        )
        return token, expires_at

    def account_for_token(self, token: str) -> Account | None:
        if not token:
            return None
        return self.store.account_for_session(session_token_hash(token))

    def revoke_token(self, token: str) -> None:
        if token:
            self.store.revoke_session(session_token_hash(token))

    def _verification_hash(self, email: str, code: str) -> str:
        return hmac.new(self.secret, f"register:{email}:{code}".encode(), hashlib.sha256).hexdigest()


def normalize_email(value: str) -> str:
    email = value.strip().casefold()
    if len(email) > 254 or not EMAIL_PATTERN.fullmatch(email):
        raise InvalidEmailError("请输入有效的邮箱地址。")
    return email


def validate_password(password: str) -> None:
    if len(password) < 8:
        raise InvalidPasswordError("密码至少需要 8 个字符。")
    if len(password) > 128:
        raise InvalidPasswordError("密码不能超过 128 个字符。")


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=32,
    )
    return "scrypt${}${}${}${}${}".format(
        SCRYPT_N,
        SCRYPT_R,
        SCRYPT_P,
        base64.urlsafe_b64encode(salt).decode(),
        base64.urlsafe_b64encode(digest).decode(),
    )


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, n, r, p, salt, expected = encoded.split("$", 5)
        if algorithm != "scrypt":
            return False
        digest = hashlib.scrypt(
            password.encode("utf-8"),
            salt=base64.urlsafe_b64decode(salt.encode()),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=32,
        )
        return hmac.compare_digest(digest, base64.urlsafe_b64decode(expected.encode()))
    except (ValueError, TypeError):
        return False


def session_token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


DUMMY_PASSWORD_HASH = hash_password("invalid-password-placeholder")
account_store = AccountStore(settings.system_path / "accounts.sqlite3")
auth_service = AuthService(account_store, VerificationMailer(settings), secret=settings.jwt_secret)
