import hashlib
import hmac
import secrets
from collections.abc import Callable

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from .db import get_db
from .models import User

_ITERATIONS = 600_000
MIN_PASSWORD_LENGTH = 12
# A syntactically valid hash used to keep failed-login work comparable.
_DUMMY_PASSWORD_HASH = (
    "pbkdf2_sha256$600000$00000000000000000000000000000000$"
    "0000000000000000000000000000000000000000000000000000000000000000"
)


def hash_password(password: str) -> str:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError("Пароль должен содержать не менее 12 символов")
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _ITERATIONS)
    return "pbkdf2_sha256${}${}${}".format(_ITERATIONS, salt.hex(), digest.hex())


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt_hex, digest_hex = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), bytes.fromhex(salt_hex), int(iterations)
        )
        return hmac.compare_digest(actual, bytes.fromhex(digest_hex))
    except ValueError:
        return False
    except TypeError:
        return False


def verify_dummy_password(password: str) -> bool:
    """Perform the password-hash work for unknown or inactive accounts."""
    return verify_password(password, _DUMMY_PASSWORD_HASH)


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User:
    user_id = request.session.get("user_id")
    if not isinstance(user_id, int):
        api_request = request.url.path.startswith("/api/")
        raise HTTPException(
            status_code=(
                status.HTTP_401_UNAUTHORIZED
                if api_request
                else status.HTTP_303_SEE_OTHER
            ),
            detail="Требуется авторизация",
            headers={} if api_request else {"Location": "/login"},
        )
    user = db.get(User, user_id)
    if user is None or not user.is_active:
        request.session.clear()
        api_request = request.url.path.startswith("/api/")
        raise HTTPException(
            status_code=(
                status.HTTP_401_UNAUTHORIZED
                if api_request
                else status.HTTP_303_SEE_OTHER
            ),
            detail="Требуется авторизация",
            headers={} if api_request else {"Location": "/login"},
        )
    return user


def require_roles(*roles: str) -> Callable:
    def role_dependency(user: User = Depends(get_current_user)) -> User:
        if user.role not in roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Недостаточно прав для выполнения действия",
            )
        return user

    return role_dependency
