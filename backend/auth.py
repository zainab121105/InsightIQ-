"""Authentication foundation for InsightIQ.

This module intentionally does not register routes. It provides security
primitives, MySQL-backed session operations, and FastAPI dependencies for the
future authentication surface.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field

from database import get_db_connection


logger = logging.getLogger(__name__)


ACCESS_TOKEN_TTL_SECONDS = 15 * 60
DEFAULT_REFRESH_TOKEN_TTL_SECONDS = 30 * 24 * 60 * 60
DEFAULT_ACCESS_COOKIE_NAME = "insightiq_access"
DUMMY_ARGON2ID_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=2$"
    "rMeNuQNMNUYnJkWyH4Io8Q$pIf0/SmByWzWLzCyCK4CRNy3gw3Hw2k6OciAA73hAC8"
)


class AuthenticationError(Exception):
    """Raised when credentials or a token are not valid."""


class AuthenticationServiceError(Exception):
    """Raised when the authentication persistence service is unavailable."""


class InvalidAccessTokenError(AuthenticationError):
    """Raised when a JWT fails validation."""


class InvalidRefreshTokenError(AuthenticationError):
    """Raised for missing, expired, revoked, or otherwise unusable refresh tokens."""


class BusinessAccessError(Exception):
    """Raised when a user cannot access the requested business context."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        parsed = int(value)
    except ValueError as error:
        raise RuntimeError(f"{name} must be an integer") from error
    if parsed <= 0:
        raise RuntimeError(f"{name} must be positive")
    return parsed


def _jwt_secret() -> bytes:
    value = os.getenv("AUTH_JWT_SECRET")
    if not value:
        raise RuntimeError("AUTH_JWT_SECRET must be configured")
    secret = value.encode("utf-8")
    if len(secret) < 32:
        raise RuntimeError("AUTH_JWT_SECRET must be at least 32 UTF-8 bytes")
    return secret


def _is_uuid(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        return False
    return str(parsed) == value.lower()


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _argon2_hasher() -> PasswordHasher:
    return PasswordHasher(
        time_cost=_env_int("AUTH_ARGON2_TIME_COST", 3),
        memory_cost=_env_int("AUTH_ARGON2_MEMORY_COST_KIB", 65536),
        parallelism=_env_int("AUTH_ARGON2_PARALLELISM", 2),
    )


def hash_password(password: str) -> str:
    """Return an Argon2id encoded password hash."""
    if not password:
        raise ValueError("password must not be empty")
    return _argon2_hasher().hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """Verify a password without exposing hashing-library errors."""
    if not password or not password_hash:
        return False
    try:
        return _argon2_hasher().verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def generate_opaque_token() -> str:
    """Generate a high-entropy token suitable for a secure cookie."""
    return secrets.token_urlsafe(48)


def hash_token(token: str) -> bytes:
    """Hash an opaque token before it is stored in MySQL."""
    return hashlib.sha256(token.encode("utf-8")).digest()


def generate_csrf_token() -> str:
    """Generate a synchronizer-token CSRF secret."""
    return secrets.token_urlsafe(32)


def hash_csrf_token(token: str) -> bytes:
    """Hash a CSRF token before comparing or storing it."""
    return hash_token(token)


def verify_csrf_token(token: str, token_hash: bytes) -> bool:
    """Constant-time verification of a submitted CSRF token."""
    if not token or not token_hash:
        return False
    return hmac.compare_digest(hash_csrf_token(token), token_hash)


def normalize_email(email: str) -> str:
    """Apply the Phase 1 trim and case-fold policy before database lookup."""
    normalized = email.strip().casefold()
    if not normalized or len(normalized) > 320:
        raise ValueError("email is invalid")
    return normalized


def create_access_token(
    *,
    user_id: int,
    session_id: str,
    access_token_jti: str,
    now: datetime | None = None,
    ttl_seconds: int = ACCESS_TOKEN_TTL_SECONDS,
) -> str:
    """Create an HS256 JWT with only authentication/session claims."""
    if not isinstance(user_id, int) or isinstance(user_id, bool) or user_id <= 0:
        raise ValueError("user_id must be positive")
    if not _is_uuid(session_id) or not _is_uuid(access_token_jti):
        raise ValueError("session_id and access_token_jti must be UUIDs")
    if not isinstance(ttl_seconds, int) or isinstance(ttl_seconds, bool):
        raise ValueError("ttl_seconds must be an integer")
    if ttl_seconds <= 0 or ttl_seconds > ACCESS_TOKEN_TTL_SECONDS:
        raise ValueError("access token lifetime exceeds the maximum")
    issued_at = now or _utc_now()
    expires_at = issued_at + timedelta(seconds=ttl_seconds)
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {
        "sub": str(user_id),
        "sid": session_id,
        "jti": access_token_jti,
        "iat": int(issued_at.replace(tzinfo=timezone.utc).timestamp()),
        "exp": int(expires_at.replace(tzinfo=timezone.utc).timestamp()),
        "type": "access",
    }
    encoded_header = _b64encode(
        json.dumps(header, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    encoded_payload = _b64encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    signing_input = f"{encoded_header}.{encoded_payload}".encode("ascii")
    signature = hmac.new(_jwt_secret(), signing_input, hashlib.sha256).digest()
    return f"{encoded_header}.{encoded_payload}.{_b64encode(signature)}"


def verify_access_token(
    token: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Verify an access JWT signature, structure, type, and expiration."""
    if not isinstance(token, str):
        raise InvalidAccessTokenError("invalid access token")
    try:
        encoded_header, encoded_payload, encoded_signature = token.split(".")
        header = json.loads(_b64decode(encoded_header))
        payload = json.loads(_b64decode(encoded_payload))
    except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError, base64.binascii.Error) as error:
        raise InvalidAccessTokenError("invalid access token") from error

    if not isinstance(header, dict) or not isinstance(payload, dict):
        raise InvalidAccessTokenError("invalid access token")
    if header.get("alg") != "HS256" or header.get("typ") != "JWT":
        raise InvalidAccessTokenError("invalid access token")

    signing_input = f"{encoded_header}.{encoded_payload}".encode("ascii")
    expected_signature = hmac.new(
        _jwt_secret(), signing_input, hashlib.sha256
    ).digest()
    try:
        supplied_signature = _b64decode(encoded_signature)
    except (ValueError, TypeError, base64.binascii.Error) as error:
        raise InvalidAccessTokenError("invalid access token") from error
    if not hmac.compare_digest(expected_signature, supplied_signature):
        raise InvalidAccessTokenError("invalid access token")

    required = ("sub", "sid", "jti", "iat", "exp", "type")
    if any(field not in payload for field in required) or payload["type"] != "access":
        raise InvalidAccessTokenError("invalid access token")
    if not all(
        isinstance(payload[field], str) and payload[field]
        for field in ("sub", "sid", "jti")
    ):
        raise InvalidAccessTokenError("invalid access token")
    if (
        not payload["sub"].isdigit()
        or int(payload["sub"]) <= 0
        or not _is_uuid(payload["sid"])
        or not _is_uuid(payload["jti"])
    ):
        raise InvalidAccessTokenError("invalid access token")
    if not all(
        isinstance(payload[field], int) and not isinstance(payload[field], bool)
        for field in ("iat", "exp")
    ):
        raise InvalidAccessTokenError("invalid access token")
    if payload["exp"] <= payload["iat"]:
        raise InvalidAccessTokenError("invalid access token")
    if payload["exp"] - payload["iat"] > ACCESS_TOKEN_TTL_SECONDS:
        raise InvalidAccessTokenError("invalid access token")

    current_timestamp = int(
        (now or _utc_now()).replace(tzinfo=timezone.utc).timestamp()
    )
    if payload["exp"] <= current_timestamp or payload["iat"] > current_timestamp + 60:
        raise InvalidAccessTokenError("invalid access token")
    return payload


@dataclass(frozen=True)
class AuthenticatedSession:
    user_id: int
    session_id: str
    access_token_jti: str
    csrf_token_hash: bytes
    expires_at: datetime


@dataclass(frozen=True)
class AuthenticationResult:
    session: AuthenticatedSession
    access_token: str
    refresh_token: str
    csrf_token: str


@dataclass(frozen=True)
class AuthenticatedBusinessContext:
    user_id: int
    business_id: int
    role: str
    session_id: str


class SignupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(..., min_length=1, max_length=320)
    password: str = Field(..., min_length=8, max_length=256)
    display_name: str = Field(..., min_length=1, max_length=150)
    business_name: str = Field(..., min_length=1, max_length=150)
    business_slug: str = Field(..., min_length=1, max_length=150)


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(..., min_length=1, max_length=320)
    password: str = Field(..., min_length=1, max_length=256)


class SignupResult:
    def __init__(self, authentication: AuthenticationResult, business_id: int):
        self.authentication = authentication
        self.business_id = business_id


ConnectionFactory = Callable[[], Any]


class AuthService:
    """MySQL-backed login, session, logout, and refresh-token operations."""

    def __init__(self, connection_factory: ConnectionFactory | None = None):
        self._connection_factory = connection_factory or get_db_connection

    @staticmethod
    def _close_cursor(cursor: Any) -> None:
        if cursor is not None:
            try:
                cursor.close()
            except Exception:
                pass

    @staticmethod
    def _close_connection(connection: Any) -> None:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass

    def _get_connection(self) -> Any:
        try:
            return self._connection_factory()
        except Exception as error:
            raise AuthenticationServiceError(
                "authentication service unavailable"
            ) from error

    @staticmethod
    def _require_affected_rows(
        cursor: Any,
        minimum: int,
        operation: str,
        maximum: int | None = None,
    ) -> None:
        rowcount = getattr(cursor, "rowcount", None)
        if (
            not isinstance(rowcount, int)
            or isinstance(rowcount, bool)
            or rowcount < 0
            or rowcount < minimum
            or (maximum is not None and rowcount > maximum)
        ):
            raise AuthenticationServiceError(
                f"unexpected authentication state transition: {operation}"
            )

    @staticmethod
    def _audit(
        cursor: Any,
        *,
        event_type: str,
        outcome: str,
        user_id: int | None = None,
        session_id: str | None = None,
        metadata: Mapping[str, str] | None = None,
    ) -> None:
        cursor.execute(
            """
            INSERT INTO security_audit_events
                (event_type, actor_user_id, target_user_id, session_id,
                 outcome, metadata_json)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                event_type,
                user_id,
                user_id,
                session_id,
                outcome,
                json.dumps(dict(metadata or {}), separators=(",", ":")),
            ),
        )

    def _create_session_with_cursor(
        self,
        cursor: Any,
        *,
        user_id: int,
        now: datetime | None = None,
    ) -> AuthenticationResult:
        issued_at = now or _utc_now()
        session_id = str(uuid.uuid4())
        access_token_jti = str(uuid.uuid4())
        token_family_id = str(uuid.uuid4())
        csrf_token = generate_csrf_token()
        refresh_token = generate_opaque_token()
        refresh_expires_at = issued_at + timedelta(
            seconds=_env_int(
                "AUTH_REFRESH_TOKEN_TTL_SECONDS",
                DEFAULT_REFRESH_TOKEN_TTL_SECONDS,
            )
        )
        access_token = create_access_token(
            user_id=user_id,
            session_id=session_id,
            access_token_jti=access_token_jti,
            now=issued_at,
        )
        cursor.execute(
            """
            INSERT INTO user_sessions
                (session_id, user_id, access_token_jti, csrf_token_hash,
                 status, issued_at, last_used_at, expires_at)
            VALUES (%s, %s, %s, %s, 'active', %s, %s, %s)
            """,
            (
                session_id,
                user_id,
                access_token_jti,
                hash_csrf_token(csrf_token),
                issued_at,
                issued_at,
                refresh_expires_at,
            ),
        )
        self._require_affected_rows(cursor, 1, "session creation", 1)
        cursor.execute(
            """
            INSERT INTO user_session_refresh_tokens
                (session_id, token_family_id, token_hash, status,
                 issued_at, expires_at)
            VALUES (%s, %s, %s, 'current', %s, %s)
            """,
            (
                session_id,
                token_family_id,
                hash_token(refresh_token),
                issued_at,
                refresh_expires_at,
            ),
        )
        self._require_affected_rows(cursor, 1, "refresh-token creation", 1)
        return AuthenticationResult(
            session=AuthenticatedSession(
                user_id=user_id,
                session_id=session_id,
                access_token_jti=access_token_jti,
                csrf_token_hash=hash_csrf_token(csrf_token),
                expires_at=refresh_expires_at,
            ),
            access_token=access_token,
            refresh_token=refresh_token,
            csrf_token=csrf_token,
        )

    def login(self, email: str, password: str) -> AuthenticationResult:
        """Verify credentials and atomically create a session and token."""
        try:
            normalized_email = normalize_email(email)
        except ValueError as error:
            raise AuthenticationError("invalid credentials") from error

        connection = self._get_connection()
        cursor = None
        try:
            cursor = connection.cursor(dictionary=True)
            cursor.execute(
                """
                SELECT user_id, password_hash, status
                FROM users
                WHERE email = %s
                LIMIT 1
                """,
                (normalized_email,),
            )
            user = cursor.fetchone()
        except Exception as error:
            self._close_connection(connection)
            raise AuthenticationServiceError("authentication service unavailable") from error
        finally:
            self._close_cursor(cursor)

        if not user:
            verify_password(password, DUMMY_ARGON2ID_HASH)
            self._close_connection(connection)
            raise AuthenticationError("invalid credentials")
        if (
            user.get("status") != "active"
            or not verify_password(password, user.get("password_hash", ""))
        ):
            self._close_connection(connection)
            raise AuthenticationError("invalid credentials")

        cursor = None
        try:
            cursor = connection.cursor()
            result = self._create_session_with_cursor(
                cursor,
                user_id=int(user["user_id"]),
            )
            connection.commit()
        except Exception as error:
            connection.rollback()
            raise AuthenticationServiceError("authentication service unavailable") from error
        finally:
            self._close_cursor(cursor)
            self._close_connection(connection)

        return result

    def signup(
        self,
        *,
        email: str,
        password: str,
        display_name: str,
        business_name: str,
        business_slug: str,
    ) -> SignupResult:
        """Create a user, business, owner membership, and session atomically."""
        try:
            normalized_email = normalize_email(email)
        except ValueError as error:
            raise AuthenticationError("unable to create account") from error
        if not display_name.strip() or not business_name.strip() or not business_slug.strip():
            raise AuthenticationError("unable to create account")

        password_hash = hash_password(password)
        now = _utc_now()
        connection = self._get_connection()
        cursor = None
        try:
            cursor = connection.cursor()
            cursor.execute(
                """
                INSERT INTO businesses (name, slug, status, created_at, updated_at)
                VALUES (%s, %s, 'active', %s, %s)
                """,
                (business_name.strip(), business_slug.strip(), now, now),
            )
            self._require_affected_rows(cursor, 1, "business creation", 1)
            business_id = cursor.lastrowid
            if not isinstance(business_id, int) or isinstance(business_id, bool) or business_id <= 0:
                raise AuthenticationServiceError("unexpected authentication state transition")

            cursor.execute(
                """
                INSERT INTO users
                    (email, display_name, password_hash, status, created_at, updated_at)
                VALUES (%s, %s, %s, 'active', %s, %s)
                """,
                (normalized_email, display_name.strip(), password_hash, now, now),
            )
            self._require_affected_rows(cursor, 1, "user creation", 1)
            user_id = cursor.lastrowid
            if not isinstance(user_id, int) or isinstance(user_id, bool) or user_id <= 0:
                raise AuthenticationServiceError("unexpected authentication state transition")

            cursor.execute(
                """
                INSERT INTO business_memberships
                    (business_id, user_id, role, status, created_at, updated_at)
                VALUES (%s, %s, 'owner', 'active', %s, %s)
                """,
                (business_id, user_id, now, now),
            )
            self._require_affected_rows(cursor, 1, "owner membership creation", 1)
            authentication = self._create_session_with_cursor(
                cursor,
                user_id=user_id,
                now=now,
            )
            connection.commit()
        except Exception as error:
            connection.rollback()
            if isinstance(error, AuthenticationServiceError):
                raise
            raise AuthenticationServiceError("unable to create account") from error
        finally:
            self._close_cursor(cursor)
            self._close_connection(connection)

        return SignupResult(authentication, business_id)

    def get_user_profile(self, user_id: int) -> dict[str, Any]:
        connection = self._get_connection()
        cursor = None
        try:
            cursor = connection.cursor(dictionary=True)
            cursor.execute(
                """
                SELECT user_id, email, display_name, status, email_verified_at
                FROM users
                WHERE user_id = %s
                LIMIT 1
                """,
                (user_id,),
            )
            profile = cursor.fetchone()
        except Exception as error:
            raise AuthenticationServiceError("authentication service unavailable") from error
        finally:
            self._close_cursor(cursor)
            self._close_connection(connection)
        if not profile or profile.get("status") != "active":
            raise AuthenticationError("invalid authentication")
        return profile

    def list_businesses(self, user_id: int) -> list[dict[str, Any]]:
        connection = self._get_connection()
        cursor = None
        try:
            cursor = connection.cursor(dictionary=True)
            cursor.execute(
                """
                SELECT b.business_id, b.name, b.slug, bm.role
                FROM business_memberships AS bm
                INNER JOIN businesses AS b ON b.business_id = bm.business_id
                WHERE bm.user_id = %s
                  AND bm.status = 'active'
                  AND b.status = 'active'
                ORDER BY b.business_id
                """,
                (user_id,),
            )
            return cursor.fetchall()
        except Exception as error:
            raise AuthenticationServiceError("authentication service unavailable") from error
        finally:
            self._close_cursor(cursor)
            self._close_connection(connection)

    def validate_access_token(self, claims: Mapping[str, Any]) -> AuthenticatedSession:
        """Validate JWT claims against the active server-side session."""
        try:
            user_id = int(claims["sub"])
            session_id = str(claims["sid"])
            access_token_jti = str(claims["jti"])
        except (KeyError, TypeError, ValueError) as error:
            raise InvalidAccessTokenError("invalid access token") from error

        connection = self._get_connection()
        cursor = None
        try:
            cursor = connection.cursor(dictionary=True)
            cursor.execute(
                """
                SELECT s.user_id, s.session_id, s.access_token_jti,
                       s.csrf_token_hash, s.expires_at
                FROM user_sessions AS s
                INNER JOIN users AS u ON u.user_id = s.user_id
                WHERE s.session_id = %s
                  AND s.user_id = %s
                  AND s.access_token_jti = %s
                  AND s.status = 'active'
                  AND u.status = 'active'
                  AND s.expires_at > %s
                LIMIT 1
                """,
                (session_id, user_id, access_token_jti, _utc_now()),
            )
            row = cursor.fetchone()
        except Exception as error:
            raise AuthenticationServiceError("authentication service unavailable") from error
        finally:
            self._close_cursor(cursor)
            self._close_connection(connection)

        if not row:
            raise InvalidAccessTokenError("invalid access token")
        return AuthenticatedSession(
            user_id=int(row["user_id"]),
            session_id=str(row["session_id"]),
            access_token_jti=str(row["access_token_jti"]),
            csrf_token_hash=bytes(row["csrf_token_hash"]),
            expires_at=row["expires_at"],
        )

    def logout(self, session_id: str, user_id: int) -> None:
        """Revoke the session and all currently usable refresh tokens."""
        connection = self._get_connection()
        cursor = None
        now = _utc_now()
        try:
            cursor = connection.cursor()
            cursor.execute(
                """
                UPDATE user_sessions
                SET status = 'revoked', revoked_at = %s,
                    revoked_reason = 'logout'
                WHERE session_id = %s AND user_id = %s
                  AND status = 'active'
                """,
                (now, session_id, user_id),
            )
            self._require_affected_rows(cursor, 1, "logout session revocation", 1)
            cursor.execute(
                """
                UPDATE user_session_refresh_tokens
                SET status = 'revoked', revoked_at = %s,
                    revoked_reason = 'logout'
                WHERE session_id = %s AND status = 'current'
                """,
                (now, session_id),
            )
            self._require_affected_rows(cursor, 0, "logout refresh-token revocation")
            connection.commit()
        except Exception as error:
            connection.rollback()
            raise AuthenticationServiceError("authentication service unavailable") from error
        finally:
            self._close_cursor(cursor)
            self._close_connection(connection)

    def refresh(self, refresh_token: str) -> AuthenticationResult:
        """Rotate a refresh token while preserving replay distinctions."""
        presented_hash = hash_token(refresh_token)
        connection = self._get_connection()
        cursor = None
        try:
            cursor = connection.cursor(dictionary=True)
            cursor.execute(
                """
                SELECT refresh_token_id, session_id, token_family_id, status,
                       expires_at
                FROM user_session_refresh_tokens
                WHERE token_hash = %s
                FOR UPDATE
                """,
                (presented_hash,),
            )
            token_row = cursor.fetchone()

            if not token_row:
                connection.rollback()
                raise InvalidRefreshTokenError("invalid refresh token")

            token_status = token_row["status"]
            if token_status == "consumed":
                self._revoke_session_for_replay(cursor, token_row, now=_utc_now())
                connection.commit()
                raise InvalidRefreshTokenError("refresh token replay detected")
            if token_status in {"revoked", "expired"}:
                connection.rollback()
                raise InvalidRefreshTokenError("refresh token is unusable")
            if token_status != "current" or token_row["expires_at"] <= _utc_now():
                now = _utc_now()
                cursor.execute(
                    """
                    UPDATE user_session_refresh_tokens
                    SET status = 'expired'
                    WHERE refresh_token_id = %s AND status = 'current'
                    """,
                    (token_row["refresh_token_id"],),
                )
                self._require_affected_rows(cursor, 1, "refresh-token expiration", 1)
                connection.commit()
                raise InvalidRefreshTokenError("refresh token is expired")

            cursor.execute(
                """
                SELECT session_id, user_id, access_token_jti, csrf_token_hash,
                       status, expires_at
                FROM user_sessions
                WHERE session_id = %s
                FOR UPDATE
                """,
                (token_row["session_id"],),
            )
            session_row = cursor.fetchone()
            if (
                not session_row
                or session_row["status"] != "active"
                or session_row["expires_at"] <= _utc_now()
            ):
                connection.rollback()
                raise InvalidRefreshTokenError("session is unusable")

            now = _utc_now()
            new_refresh_token = generate_opaque_token()
            new_csrf_token = generate_csrf_token()
            new_access_jti = str(uuid.uuid4())
            new_refresh_expires_at = min(
                session_row["expires_at"],
                now
                + timedelta(
                    seconds=_env_int(
                        "AUTH_REFRESH_TOKEN_TTL_SECONDS",
                        DEFAULT_REFRESH_TOKEN_TTL_SECONDS,
                    )
                ),
            )
            cursor.execute(
                """
                UPDATE user_session_refresh_tokens
                SET status = 'consumed', consumed_at = %s
                WHERE refresh_token_id = %s AND status = 'current'
                """,
                (now, token_row["refresh_token_id"]),
            )
            self._require_affected_rows(cursor, 1, "refresh-token consumption", 1)
            cursor.execute(
                """
                INSERT INTO user_session_refresh_tokens
                    (session_id, token_family_id, token_hash, status,
                     issued_at, expires_at, replaces_token_id)
                VALUES (%s, %s, %s, 'current', %s, %s, %s)
                """,
                (
                    token_row["session_id"],
                    token_row["token_family_id"],
                    hash_token(new_refresh_token),
                    now,
                    new_refresh_expires_at,
                    token_row["refresh_token_id"],
                ),
            )
            self._require_affected_rows(cursor, 1, "refresh-token rotation", 1)
            new_refresh_id = cursor.lastrowid
            if new_refresh_id == token_row["refresh_token_id"]:
                raise AuthenticationServiceError("invalid refresh-token chain")
            cursor.execute(
                """
                UPDATE user_session_refresh_tokens
                SET replaced_by_token_id = %s
                WHERE refresh_token_id = %s
                  AND status = 'consumed'
                """,
                (new_refresh_id, token_row["refresh_token_id"]),
            )
            self._require_affected_rows(cursor, 1, "refresh-token chain linking", 1)
            cursor.execute(
                """
                UPDATE user_sessions
                SET access_token_jti = %s, csrf_token_hash = %s,
                    last_used_at = %s
                WHERE session_id = %s AND status = 'active'
                """,
                (
                    new_access_jti,
                    hash_csrf_token(new_csrf_token),
                    now,
                    token_row["session_id"],
                ),
            )
            self._require_affected_rows(cursor, 1, "session token update", 1)
            connection.commit()
        except InvalidRefreshTokenError:
            raise
        except Exception as error:
            connection.rollback()
            raise AuthenticationServiceError("authentication service unavailable") from error
        finally:
            self._close_cursor(cursor)
            self._close_connection(connection)

        access_token = create_access_token(
            user_id=int(session_row["user_id"]),
            session_id=str(session_row["session_id"]),
            access_token_jti=new_access_jti,
            now=now,
        )
        return AuthenticationResult(
            session=AuthenticatedSession(
                user_id=int(session_row["user_id"]),
                session_id=str(session_row["session_id"]),
                access_token_jti=new_access_jti,
                csrf_token_hash=hash_csrf_token(new_csrf_token),
                expires_at=new_refresh_expires_at,
            ),
            access_token=access_token,
            refresh_token=new_refresh_token,
            csrf_token=new_csrf_token,
        )

    def validate_refresh_csrf(self, refresh_token: str, csrf_token: str) -> bool:
        if not refresh_token or not csrf_token:
            return False
        connection = self._get_connection()
        cursor = None
        try:
            cursor = connection.cursor(dictionary=True)
            cursor.execute(
                """
                SELECT s.csrf_token_hash
                FROM user_session_refresh_tokens AS rt
                INNER JOIN user_sessions AS s ON s.session_id = rt.session_id
                WHERE rt.token_hash = %s
                  AND rt.status = 'current'
                  AND s.status = 'active'
                LIMIT 1
                """,
                (hash_token(refresh_token),),
            )
            row = cursor.fetchone()
        except Exception as error:
            raise AuthenticationServiceError("authentication service unavailable") from error
        finally:
            self._close_cursor(cursor)
            self._close_connection(connection)

    def rotate_csrf_token(self, session_id: str, user_id: int) -> str:
        """Rotate the session CSRF secret for the bootstrap endpoint."""
        csrf_token = generate_csrf_token()
        connection = self._get_connection()
        cursor = None
        try:
            cursor = connection.cursor()
            cursor.execute(
                """
                UPDATE user_sessions
                SET csrf_token_hash = %s
                WHERE session_id = %s AND user_id = %s AND status = 'active'
                """,
                (hash_csrf_token(csrf_token), session_id, user_id),
            )
            self._require_affected_rows(cursor, 1, "csrf-token rotation", 1)
            connection.commit()
            return csrf_token
        except Exception as error:
            connection.rollback()
            if isinstance(error, AuthenticationServiceError):
                raise
            raise AuthenticationServiceError(
                "authentication service unavailable"
            ) from error
        finally:
            self._close_cursor(cursor)
            self._close_connection(connection)
        return bool(row and verify_csrf_token(csrf_token, bytes(row["csrf_token_hash"])))

    def _revoke_session_for_replay(
        self,
        cursor: Any,
        token_row: Mapping[str, Any],
        *,
        now: datetime,
    ) -> None:
        """Revoke the session and currently usable tokens in one transaction."""
        cursor.execute(
            """
            UPDATE user_sessions
            SET status = 'revoked', revoked_at = %s,
                revoked_reason = 'refresh_token_replay'
            WHERE session_id = %s AND status = 'active'
            """,
            (now, token_row["session_id"]),
        )
        self._require_affected_rows(cursor, 1, "replay session revocation", 1)
        cursor.execute(
            """
            UPDATE user_session_refresh_tokens
            SET status = 'revoked', revoked_at = %s,
                revoked_reason = 'refresh_token_replay'
            WHERE session_id = %s AND status = 'current'
            """,
            (now, token_row["session_id"]),
        )
        self._require_affected_rows(cursor, 0, "replay refresh-token revocation")
        self._audit(
            cursor,
            event_type="refresh_token_replay",
            outcome="denied",
            session_id=str(token_row["session_id"]),
            metadata={"reason": "consumed_token_reuse"},
        )

    def resolve_business_context(
        self,
        *,
        user_id: int,
        session_id: str,
        business_id: int | None = None,
    ) -> AuthenticatedBusinessContext:
        """Resolve only an active membership in an active business."""
        if not isinstance(user_id, int) or isinstance(user_id, bool) or user_id <= 0:
            raise BusinessAccessError("business access denied")
        if business_id is not None and (
            not isinstance(business_id, int)
            or isinstance(business_id, bool)
            or business_id <= 0
        ):
            raise BusinessAccessError("business access denied")
        if not _is_uuid(session_id):
            raise BusinessAccessError("business access denied")
        connection = self._get_connection()
        cursor = None
        try:
            cursor = connection.cursor(dictionary=True)
            query = """
                SELECT bm.business_id, bm.user_id, bm.role
                FROM business_memberships AS bm
                INNER JOIN businesses AS b ON b.business_id = bm.business_id
                WHERE bm.user_id = %s
                  AND bm.status = 'active'
                  AND b.status = 'active'
            """
            params: list[Any] = [user_id]
            if business_id is not None:
                query += " AND bm.business_id = %s"
                params.append(business_id)
            query += " ORDER BY bm.business_id"
            cursor.execute(query, tuple(params))
            memberships = cursor.fetchall()
        except Exception as error:
            raise AuthenticationServiceError("authentication service unavailable") from error
        finally:
            self._close_cursor(cursor)
            self._close_connection(connection)

        if not memberships:
            raise BusinessAccessError("business access denied")
        if business_id is None and len(memberships) != 1:
            raise BusinessAccessError("business selection required")
        selected = memberships[0]
        return AuthenticatedBusinessContext(
            user_id=int(selected["user_id"]),
            business_id=int(selected["business_id"]),
            role=str(selected["role"]),
            session_id=session_id,
        )


def _access_token_from_request(request: Request) -> str | None:
    cookie_name = os.getenv("AUTH_ACCESS_COOKIE_NAME", DEFAULT_ACCESS_COOKIE_NAME)
    token = request.cookies.get(cookie_name)
    if token:
        return token
    authorization = request.headers.get("Authorization", "")
    if authorization.startswith("Bearer "):
        return authorization[7:].strip() or None
    return None


def get_auth_service() -> AuthService:
    """FastAPI dependency factory kept separate from route registration."""
    return AuthService()


def get_authenticated_session(
    request: Request,
    service: AuthService = Depends(get_auth_service),
) -> AuthenticatedSession:
    """Return a session validated by both JWT claims and MySQL state."""
    token = _access_token_from_request(request)
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="authentication required")
    try:
        claims = verify_access_token(token)
        return service.validate_access_token(claims)
    except (AuthenticationError, AuthenticationServiceError) as error:
        if isinstance(error, AuthenticationServiceError):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="authentication service unavailable",
            ) from error
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid authentication",
        ) from error


def get_authenticated_business_context(
    request: Request,
    session: AuthenticatedSession = Depends(get_authenticated_session),
    service: AuthService = Depends(get_auth_service),
) -> AuthenticatedBusinessContext:
    """Resolve the selected business only after session authentication."""
    raw_business_id = request.query_params.get("business_id")
    try:
        business_id = int(raw_business_id) if raw_business_id is not None else None
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="business access denied") from error
    try:
        return service.resolve_business_context(
            user_id=session.user_id,
            session_id=session.session_id,
            business_id=business_id,
        )
    except BusinessAccessError as error:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="business access denied",
        ) from error
    except AuthenticationServiceError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="authentication service unavailable",
        ) from error


def require_csrf(
    request: Request,
    session: AuthenticatedSession = Depends(get_authenticated_session),
) -> AuthenticatedSession:
    """Require the synchronizer token and configured Origin for state changes."""
    origin = request.headers.get("Origin")
    allowed_origins = {
        value.strip()
        for value in os.getenv("AUTH_ALLOWED_ORIGINS", "").split(",")
        if value.strip()
    }
    if not origin or origin not in allowed_origins:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="CSRF validation failed")
    token = request.headers.get("X-CSRF-Token")
    if not token or not verify_csrf_token(token, session.csrf_token_hash):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="CSRF validation failed")
    return session


def allowed_origins() -> set[str]:
    configured = {
        value.strip()
        for value in os.getenv("AUTH_ALLOWED_ORIGINS", "").split(",")
        if value.strip()
    }
    return configured or {"http://localhost:5173", "http://127.0.0.1:5173"}


def _cookie_options(path: str) -> dict[str, Any]:
    same_site = os.getenv("AUTH_COOKIE_SAMESITE", "lax").lower()
    if same_site not in {"lax", "strict", "none"}:
        raise RuntimeError("AUTH_COOKIE_SAMESITE must be lax, strict, or none")
    configured_secure = os.getenv("AUTH_COOKIE_SECURE")
    environment = os.getenv("APP_ENV", "development").lower()
    if configured_secure is None and environment in {"production", "prod"}:
        raise RuntimeError("AUTH_COOKIE_SECURE must be configured in production")
    secure = (configured_secure or "false").lower() in {"1", "true", "yes"}
    if environment in {"production", "prod"} and not secure:
        raise RuntimeError("AUTH_COOKIE_SECURE must be enabled in production")
    if same_site == "none" and not secure:
        raise RuntimeError("AUTH_COOKIE_SECURE must be enabled with SameSite=None")
    return {
        "secure": secure,
        "httponly": True,
        "samesite": same_site,
        "domain": os.getenv("AUTH_COOKIE_DOMAIN") or None,
        "path": path,
    }


def _set_auth_cookies(response: Response, result: AuthenticationResult) -> None:
    access_name = os.getenv("AUTH_ACCESS_COOKIE_NAME", DEFAULT_ACCESS_COOKIE_NAME)
    refresh_name = os.getenv("AUTH_REFRESH_COOKIE_NAME", "insightiq_refresh")
    response.set_cookie(
        access_name,
        result.access_token,
        max_age=ACCESS_TOKEN_TTL_SECONDS,
        **_cookie_options("/"),
    )
    response.set_cookie(
        refresh_name,
        result.refresh_token,
        max_age=_env_int("AUTH_REFRESH_TOKEN_TTL_SECONDS", DEFAULT_REFRESH_TOKEN_TTL_SECONDS),
        **_cookie_options("/auth"),
    )


def _clear_auth_cookies(response: Response) -> None:
    access_name = os.getenv("AUTH_ACCESS_COOKIE_NAME", DEFAULT_ACCESS_COOKIE_NAME)
    refresh_name = os.getenv("AUTH_REFRESH_COOKIE_NAME", "insightiq_refresh")
    response.delete_cookie(access_name, **_cookie_options("/"))
    response.delete_cookie(refresh_name, **_cookie_options("/auth"))


def _require_allowed_origin(request: Request) -> None:
    if request.headers.get("Origin") not in allowed_origins():
        raise HTTPException(status_code=403, detail="CSRF validation failed")


def _auth_error(error: Exception) -> HTTPException:
    if isinstance(error, AuthenticationServiceError):
        return HTTPException(status_code=503, detail="authentication service unavailable")
    if isinstance(error, (AuthenticationError, BusinessAccessError)):
        return HTTPException(status_code=401, detail="authentication failed")
    return HTTPException(status_code=500, detail="authentication failed")


auth_router = APIRouter(prefix="/auth", tags=["authentication"])


@auth_router.post("/signup")
def signup(
    payload: SignupRequest,
    response: Response,
    service: AuthService = Depends(get_auth_service),
) -> dict[str, Any]:
    try:
        result = service.signup(
            email=payload.email,
            password=payload.password,
            display_name=payload.display_name,
            business_name=payload.business_name,
            business_slug=payload.business_slug,
        )
        _set_auth_cookies(response, result.authentication)
        return {
            "user_id": result.authentication.session.user_id,
            "business_id": result.business_id,
            "csrf_token": result.authentication.csrf_token,
        }
    except Exception as error:
        logger.exception("Authentication signup failed")
        raise _auth_error(error) from error


@auth_router.post("/login")
def login(
    payload: LoginRequest,
    response: Response,
    service: AuthService = Depends(get_auth_service),
) -> dict[str, Any]:
    try:
        result = service.login(payload.email, payload.password)
        _set_auth_cookies(response, result)
        return {"user_id": result.session.user_id, "csrf_token": result.csrf_token}
    except Exception as error:
        logger.exception("Authentication login failed")
        raise _auth_error(error) from error


@auth_router.post("/refresh")
def refresh(
    request: Request,
    response: Response,
    service: AuthService = Depends(get_auth_service),
) -> dict[str, Any]:
    _require_allowed_origin(request)
    refresh_name = os.getenv("AUTH_REFRESH_COOKIE_NAME", "insightiq_refresh")
    refresh_token = request.cookies.get(refresh_name)
    csrf_token = request.headers.get("X-CSRF-Token", "")
    if not refresh_token or not csrf_token:
        raise HTTPException(status_code=401, detail="authentication required")
    try:
        if not service.validate_refresh_csrf(refresh_token, csrf_token):
            raise HTTPException(status_code=403, detail="CSRF validation failed")
        result = service.refresh(refresh_token)
        _set_auth_cookies(response, result)
        return {"user_id": result.session.user_id, "csrf_token": result.csrf_token}
    except HTTPException:
        raise
    except Exception as error:
        raise _auth_error(error) from error


@auth_router.post("/csrf")
def csrf_bootstrap(
    response: Response,
    request: Request,
    session: AuthenticatedSession = Depends(get_authenticated_session),
    service: AuthService = Depends(get_auth_service),
) -> dict[str, str]:
    """Issue a fresh CSRF token for an already authenticated browser session."""
    _require_allowed_origin(request)
    try:
        csrf_token = service.rotate_csrf_token(
            session.session_id,
            session.user_id,
        )
        response.headers["Cache-Control"] = "no-store"
        return {"csrf_token": csrf_token}
    except Exception as error:
        raise _auth_error(error) from error


@auth_router.post("/logout")
def logout(
    response: Response,
    session: AuthenticatedSession = Depends(require_csrf),
    service: AuthService = Depends(get_auth_service),
) -> dict[str, str]:
    try:
        service.logout(session.session_id, session.user_id)
        _clear_auth_cookies(response)
        return {"status": "logged_out"}
    except Exception as error:
        raise _auth_error(error) from error


@auth_router.get("/me")
def me(
    session: AuthenticatedSession = Depends(get_authenticated_session),
    service: AuthService = Depends(get_auth_service),
) -> dict[str, Any]:
    try:
        profile = service.get_user_profile(session.user_id)
        return {"user": profile, "session_id": session.session_id}
    except Exception as error:
        raise _auth_error(error) from error


@auth_router.get("/businesses")
def businesses(
    session: AuthenticatedSession = Depends(get_authenticated_session),
    service: AuthService = Depends(get_auth_service),
) -> list[dict[str, Any]]:
    try:
        return service.list_businesses(session.user_id)
    except Exception as error:
        raise _auth_error(error) from error
