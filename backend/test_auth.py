"""Database-free tests for the authentication foundation."""

import os
import json
import sys
import unittest
from datetime import datetime, timedelta
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(__file__))
database_stub = ModuleType("database")
database_stub.get_db_connection = MagicMock()
sys.modules.setdefault("database", database_stub)

from auth import (  # noqa: E402
    AuthenticatedSession,
    AuthService,
    AuthenticationError,
    AuthenticationServiceError,
    BusinessAccessError,
    InvalidAccessTokenError,
    InvalidRefreshTokenError,
    create_access_token,
    generate_csrf_token,
    generate_opaque_token,
    hash_csrf_token,
    hash_password,
    hash_token,
    require_csrf,
    _jwt_secret,
    verify_access_token,
    verify_csrf_token,
    verify_password,
)
import auth as auth_module  # noqa: E402
from starlette.requests import Request  # noqa: E402
from fastapi import HTTPException  # noqa: E402


SESSION_ID = "00000000-0000-4000-8000-000000000001"
JTI = "00000000-0000-4000-8000-000000000002"


def _request(headers=None, query=None):
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/test",
        "query_string": b"",
        "headers": [
            (key.lower().encode("ascii"), value.encode("ascii"))
            for key, value in (headers or {}).items()
        ],
    }
    return Request(scope)


def _signed_payload_token(payload, header=None):
    header = header or {"alg": "HS256", "typ": "JWT"}
    encoded_header = auth_module._b64encode(
        json.dumps(header, separators=(",", ":"), sort_keys=True).encode()
    )
    encoded_payload = auth_module._b64encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    )
    signing_input = f"{encoded_header}.{encoded_payload}".encode("ascii")
    signature = auth_module.hmac.new(
        auth_module._jwt_secret(), signing_input, auth_module.hashlib.sha256
    ).digest()
    return f"{encoded_header}.{encoded_payload}.{auth_module._b64encode(signature)}"


class TestSecurityUtilities(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(
            os.environ,
            {"AUTH_JWT_SECRET": "test-only-secret-0123456789abcdef"},
            clear=False,
        )
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def test_argon2id_hash_and_verify(self):
        password_hash = hash_password("correct horse battery staple")
        self.assertTrue(password_hash.startswith("$argon2id$"))
        self.assertTrue(verify_password("correct horse battery staple", password_hash))
        self.assertFalse(verify_password("wrong", password_hash))

    def test_opaque_and_csrf_tokens_are_hashed(self):
        token = generate_opaque_token()
        self.assertNotEqual(token.encode(), hash_token(token))
        csrf = generate_csrf_token()
        csrf_hash = hash_csrf_token(csrf)
        self.assertTrue(verify_csrf_token(csrf, csrf_hash))
        self.assertFalse(verify_csrf_token("wrong", csrf_hash))

    def test_access_token_round_trip_and_expiration(self):
        now = datetime(2026, 11, 2, 0, 0, 0)
        token = create_access_token(
            user_id=7,
            session_id=SESSION_ID,
            access_token_jti=JTI,
            now=now,
        )
        claims = verify_access_token(token, now=now + timedelta(seconds=1))
        self.assertEqual(claims["sub"], "7")
        self.assertEqual(claims["sid"], SESSION_ID)
        with self.assertRaises(InvalidAccessTokenError):
            verify_access_token(token, now=now + timedelta(minutes=16))

    def test_jwt_secret_requires_at_least_32_utf8_bytes(self):
        with patch.dict(os.environ, {"AUTH_JWT_SECRET": "short"}, clear=False):
            with self.assertRaisesRegex(RuntimeError, "at least 32 UTF-8 bytes"):
                _jwt_secret()

        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "must be configured"):
                _jwt_secret()


class TestAuthService(unittest.TestCase):
    def _connection(self, fetchone_values=(), fetchall_values=(), lastrowid=2):
        connection = MagicMock()
        cursor = MagicMock()
        cursor.rowcount = 1
        cursor.fetchone.side_effect = list(fetchone_values)
        cursor.fetchall.side_effect = list(fetchall_values)
        cursor.lastrowid = lastrowid
        connection.cursor.return_value = cursor
        return connection, cursor

    def test_rotate_csrf_token_updates_hash_transactionally(self):
        connection, cursor = self._connection()
        service = AuthService(lambda: connection)

        with patch("auth.generate_csrf_token", return_value="fresh-csrf"):
            result = service.rotate_csrf_token(SESSION_ID, 7)

        self.assertEqual(result, "fresh-csrf")
        query, parameters = cursor.execute.call_args.args
        self.assertIn("UPDATE user_sessions", query)
        self.assertEqual(parameters[1:], (SESSION_ID, 7))
        self.assertEqual(parameters[0], hash_csrf_token("fresh-csrf"))
        connection.commit.assert_called_once()
        connection.rollback.assert_not_called()

    @patch.dict(
        os.environ,
        {"AUTH_JWT_SECRET": "test-only-secret-0123456789abcdef"},
        clear=False,
    )
    @patch("auth.verify_password", return_value=True)
    def test_login_creates_session_and_refresh_record(self, verify):
        connection, cursor = self._connection(
            fetchone_values=[{"user_id": 7, "password_hash": "hash", "status": "active"}]
        )
        service = AuthService(lambda: connection)

        result = service.login(" User@example.com ", "password")

        verify.assert_called_once_with("password", "hash")
        self.assertEqual(result.session.user_id, 7)
        self.assertTrue(result.access_token)
        self.assertTrue(result.refresh_token)
        self.assertTrue(result.csrf_token)
        connection.commit.assert_called_once()
        self.assertEqual(cursor.execute.call_count, 3)
        connection.close.assert_called_once()

    @patch.dict(
        os.environ,
        {"AUTH_JWT_SECRET": "test-only-secret-0123456789abcdef"},
        clear=False,
    )
    @patch("auth.hash_password", return_value="argon2-hash")
    def test_signup_commits_account_and_session_as_one_transaction(self, _hash_password):
        connection, cursor = self._connection(lastrowid=7)

        result = AuthService(lambda: connection).signup(
            email="owner@example.com",
            password="password",
            display_name="Owner",
            business_name="Acme",
            business_slug="acme",
        )

        statements = [call.args[0] for call in cursor.execute.call_args_list]
        self.assertEqual(len(statements), 5)
        self.assertTrue(any("INSERT INTO businesses" in statement for statement in statements))
        self.assertTrue(any("INSERT INTO users" in statement for statement in statements))
        self.assertTrue(any("INSERT INTO business_memberships" in statement for statement in statements))
        self.assertTrue(any("INSERT INTO user_sessions" in statement for statement in statements))
        self.assertTrue(any("INSERT INTO user_session_refresh_tokens" in statement for statement in statements))
        self.assertEqual(result.business_id, 7)
        connection.commit.assert_called_once()
        connection.rollback.assert_not_called()

    @patch.dict(
        os.environ,
        {"AUTH_JWT_SECRET": "test-only-secret-0123456789abcdef"},
        clear=False,
    )
    @patch("auth.hash_password", return_value="argon2-hash")
    def test_signup_session_failure_rolls_back_all_records(self, _hash_password):
        connection, cursor = self._connection(lastrowid=7)
        cursor.execute.side_effect = [None, None, None, RuntimeError("session insert failed")]

        with self.assertRaises(AuthenticationServiceError):
            AuthService(lambda: connection).signup(
                email="owner@example.com",
                password="password",
                display_name="Owner",
                business_name="Acme",
                business_slug="acme",
            )

        connection.rollback.assert_called_once()
        connection.commit.assert_not_called()

    def test_consumed_refresh_token_revokes_session_and_audits(self):
        connection, cursor = self._connection(
            fetchone_values=[{
                "refresh_token_id": 3,
                "session_id": SESSION_ID,
                "token_family_id": "family-1",
                "status": "consumed",
                "expires_at": datetime(2026, 11, 3),
            }]
        )
        service = AuthService(lambda: connection)

        with self.assertRaises(InvalidRefreshTokenError):
            service.refresh("presented-token")

        self.assertTrue(any("security_audit_events" in call.args[0] for call in cursor.execute.call_args_list))
        replay_updates = [
            call.args[0]
            for call in cursor.execute.call_args_list
            if "UPDATE user_session_refresh_tokens" in call.args[0]
        ]
        self.assertTrue(replay_updates)
        self.assertTrue(any("session_id = %s" in query and "status = 'current'" in query for query in replay_updates))
        connection.commit.assert_called_once()
        connection.close.assert_called_once()

    def test_affected_row_count_requires_exact_integer_range(self):
        cursor = MagicMock()

        cursor.rowcount = 1
        AuthService._require_affected_rows(cursor, 1, "test", 1)

        with self.assertRaises(AuthenticationServiceError):
            AuthService._require_affected_rows(SimpleNamespace(), 0, "test")

        for invalid_count in (None, -1, True, False):
            cursor.rowcount = invalid_count
            with self.assertRaises(AuthenticationServiceError):
                AuthService._require_affected_rows(cursor, 0, "test")

        cursor.rowcount = 2
        with self.assertRaises(AuthenticationServiceError):
            AuthService._require_affected_rows(cursor, 1, "test", 1)

    def test_affected_row_count_allows_zero_or_more_when_unbounded(self):
        cursor = MagicMock()
        cursor.rowcount = 0
        AuthService._require_affected_rows(cursor, 0, "test")
        cursor.rowcount = 3
        AuthService._require_affected_rows(cursor, 0, "test")

    def test_logout_unexpected_session_transition_rolls_back(self):
        connection, cursor = self._connection()
        cursor.rowcount = 0

        with self.assertRaises(AuthenticationServiceError):
            AuthService(lambda: connection).logout(SESSION_ID, 7)

        connection.rollback.assert_called_once()
        connection.commit.assert_not_called()

    @patch("auth.verify_password", return_value=False)
    def test_missing_user_runs_dummy_argon2_verification(self, verify):
        connection, _ = self._connection(fetchone_values=[None])

        with self.assertRaises(AuthenticationError):
            AuthService(lambda: connection).login("missing@example.com", "password")

        verify.assert_called_once()
        self.assertNotEqual(verify.call_args.args[1], "password")

    @patch.dict(
        os.environ,
        {"AUTH_JWT_SECRET": "test-only-secret-0123456789abcdef"},
        clear=False,
    )
    def test_current_refresh_token_rotates_transactionally(self):
        connection, cursor = self._connection(
            fetchone_values=[
                {
                    "refresh_token_id": 3,
                    "session_id": SESSION_ID,
                    "token_family_id": "family-1",
                    "status": "current",
                    "expires_at": datetime(2026, 11, 3),
                },
                {
                    "session_id": SESSION_ID,
                    "user_id": 7,
                    "access_token_jti": JTI,
                    "csrf_token_hash": b"hash",
                    "status": "active",
                    "expires_at": datetime(2026, 11, 3),
                },
            ],
            lastrowid=4,
        )
        service = AuthService(lambda: connection)

        result = service.refresh("presented-token")

        self.assertEqual(result.session.session_id, SESSION_ID)
        self.assertTrue(result.refresh_token)
        self.assertTrue(result.csrf_token)
        self.assertEqual(
            hash_csrf_token(result.csrf_token),
            result.session.csrf_token_hash,
        )
        connection.commit.assert_called_once()
        self.assertTrue(
            any(
                "replaces_token_id" in call.args[0]
                for call in cursor.execute.call_args_list
            )
        )

    def test_active_session_validation_uses_database_state(self):
        connection, cursor = self._connection(
            fetchone_values=[{
                "user_id": 7,
                "session_id": SESSION_ID,
                "access_token_jti": JTI,
                "csrf_token_hash": b"hash",
                "expires_at": datetime(2026, 11, 3),
            }]
        )
        service = AuthService(lambda: connection)

        session = service.validate_access_token({
            "sub": "7",
            "sid": SESSION_ID,
            "jti": JTI,
        })

        self.assertEqual(session.session_id, SESSION_ID)
        self.assertTrue(any("status = 'active'" in call.args[0] for call in cursor.execute.call_args_list))


class TestJWTValidation(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(
            os.environ,
            {"AUTH_JWT_SECRET": "test-only-secret-0123456789abcdef"},
            clear=False,
        )
        self.env.start()
        self.now = datetime(2026, 11, 2, 0, 0, 0)
        timestamp = int(self.now.replace(tzinfo=auth_module.timezone.utc).timestamp())
        self.base = {
            "sub": "7",
            "sid": SESSION_ID,
            "jti": JTI,
            "iat": timestamp,
            "exp": timestamp + 900,
            "type": "access",
        }

    def tearDown(self):
        self.env.stop()

    def test_tampered_signature_and_wrong_algorithm(self):
        token = create_access_token(
            user_id=7, session_id=SESSION_ID, access_token_jti=JTI, now=self.now
        )
        parts = token.split(".")
        parts[2] = "tampered"
        with self.assertRaises(InvalidAccessTokenError):
            verify_access_token(".".join(parts), now=self.now)
        with self.assertRaises(InvalidAccessTokenError):
            verify_access_token(
                _signed_payload_token(self.base, {"alg": "none", "typ": "JWT"}),
                now=self.now,
            )

    def test_malformed_payload_and_invalid_claims(self):
        malformed = (
            f"{auth_module._b64encode(b'{\"alg\":\"HS256\",\"typ\":\"JWT\"}')}"
            f".{auth_module._b64encode(b'not-json')}.x"
        )
        with self.assertRaises(InvalidAccessTokenError):
            verify_access_token(malformed, now=self.now)
        invalid_payloads = (
            {**self.base, "sub": "0"},
            {**self.base, "sub": "-1"},
            {**self.base, "iat": True},
            {**self.base, "exp": self.base["iat"]},
            {**self.base, "exp": self.base["iat"] + 901},
            {**self.base, "iat": self.base["exp"] + 1},
            {**self.base, "sid": "bad-session"},
            {**self.base, "jti": "bad-jti"},
        )
        for payload in invalid_payloads:
            with self.assertRaises(InvalidAccessTokenError):
                verify_access_token(_signed_payload_token(payload), now=self.now)


class TestAdditionalAuthServiceRules(unittest.TestCase):
    def _connection(self, fetchone_values=(), fetchall_values=(), lastrowid=4):
        connection = MagicMock()
        cursor = MagicMock()
        cursor.rowcount = 1
        cursor.fetchone.side_effect = list(fetchone_values)
        cursor.fetchall.side_effect = list(fetchall_values)
        cursor.lastrowid = lastrowid
        connection.cursor.return_value = cursor
        return connection, cursor

    @patch.dict(
        os.environ,
        {"AUTH_JWT_SECRET": "test-only-secret-0123456789abcdef"},
        clear=False,
    )
    def test_refresh_renews_csrf_and_updates_hash_in_transaction(self):
        connection, cursor = self._connection(
            fetchone_values=[
                {
                    "refresh_token_id": 3,
                    "session_id": SESSION_ID,
                    "token_family_id": "family-1",
                    "status": "current",
                    "expires_at": datetime(2026, 11, 3),
                },
                {
                    "session_id": SESSION_ID,
                    "user_id": 7,
                    "access_token_jti": JTI,
                    "csrf_token_hash": b"old-hash",
                    "status": "active",
                    "expires_at": datetime(2026, 11, 3),
                },
            ]
        )
        result = AuthService(lambda: connection).refresh("presented-token")

        self.assertTrue(result.csrf_token)
        self.assertEqual(hash_csrf_token(result.csrf_token), result.session.csrf_token_hash)
        session_update = next(
            call.args[0]
            for call in cursor.execute.call_args_list
            if "UPDATE user_sessions" in call.args[0]
            and "csrf_token_hash" in call.args[0]
        )
        self.assertIn("csrf_token_hash", session_update)
        connection.commit.assert_called_once()

    @patch.dict(
        os.environ,
        {"AUTH_JWT_SECRET": "test-only-secret-0123456789abcdef"},
        clear=False,
    )
    def test_refresh_failure_rolls_back_and_closes_connection(self):
        connection, cursor = self._connection(
            fetchone_values=[
                {
                    "refresh_token_id": 3,
                    "session_id": SESSION_ID,
                    "token_family_id": "family-1",
                    "status": "current",
                    "expires_at": datetime(2026, 11, 3),
                },
                {
                    "session_id": SESSION_ID,
                    "user_id": 7,
                    "access_token_jti": JTI,
                    "csrf_token_hash": b"old-hash",
                    "status": "active",
                    "expires_at": datetime(2026, 11, 3),
                },
            ]
        )
        cursor.execute.side_effect = [None, None, None, RuntimeError("SQL detail")]

        with self.assertRaises(AuthenticationServiceError) as raised:
            AuthService(lambda: connection).refresh("presented-token")

        self.assertEqual(str(raised.exception), "authentication service unavailable")
        connection.rollback.assert_called_once()
        connection.close.assert_called_once()

    def test_replay_revokes_all_current_session_tokens(self):
        connection, cursor = self._connection(
            fetchone_values=[{
                "refresh_token_id": 3,
                "session_id": SESSION_ID,
                "token_family_id": "family-1",
                "status": "consumed",
                "expires_at": datetime(2026, 11, 3),
            }]
        )
        with self.assertRaises(InvalidRefreshTokenError):
            AuthService(lambda: connection).refresh("presented-token")
        replay_update = next(
            call.args[0]
            for call in cursor.execute.call_args_list
            if "UPDATE user_session_refresh_tokens" in call.args[0]
        )
        self.assertIn("WHERE session_id = %s AND status = 'current'", replay_update)
        self.assertNotIn("token_family_id = %s", replay_update)

    def test_logout_revokes_session_and_current_tokens(self):
        connection, cursor = self._connection()
        AuthService(lambda: connection).logout(SESSION_ID, 7)
        statements = [call.args[0] for call in cursor.execute.call_args_list]
        self.assertTrue(any("UPDATE user_sessions" in statement for statement in statements))
        self.assertTrue(any("UPDATE user_session_refresh_tokens" in statement for statement in statements))
        connection.commit.assert_called_once()

    def test_business_selection_rules(self):
        connection, _ = self._connection(
            fetchall_values=[[{"business_id": 9, "user_id": 7, "role": "owner"}]]
        )
        context = AuthService(lambda: connection).resolve_business_context(
            user_id=7, session_id=SESSION_ID, business_id=9
        )
        self.assertEqual(context.business_id, 9)

        for business_id in (0, -1):
            with self.assertRaises(BusinessAccessError):
                AuthService(lambda: MagicMock()).resolve_business_context(
                    user_id=7, session_id=SESSION_ID, business_id=business_id
                )

        connection, _ = self._connection(fetchall_values=[[]])
        with self.assertRaises(BusinessAccessError):
            AuthService(lambda: connection).resolve_business_context(
                user_id=7, session_id=SESSION_ID, business_id=10
            )

        connection, _ = self._connection(fetchall_values=[[
            {"business_id": 9, "user_id": 7, "role": "owner"},
            {"business_id": 10, "user_id": 7, "role": "member"},
        ]])
        with self.assertRaises(BusinessAccessError):
            AuthService(lambda: connection).resolve_business_context(
                user_id=7, session_id=SESSION_ID
            )

    def test_database_failure_does_not_leak_details(self):
        service = AuthService(lambda: (_ for _ in ()).throw(RuntimeError("secret SQL details")))
        with self.assertRaises(AuthenticationServiceError) as raised:
            service.logout(SESSION_ID, 7)
        self.assertEqual(str(raised.exception), "authentication service unavailable")


class TestCSRFDependency(unittest.TestCase):
    def test_invalid_token_and_origin_are_rejected(self):
        session = AuthenticatedSession(
            user_id=7,
            session_id=SESSION_ID,
            access_token_jti=JTI,
            csrf_token_hash=hash_csrf_token("correct"),
            expires_at=datetime(2026, 11, 3),
        )
        with patch.dict(os.environ, {"AUTH_ALLOWED_ORIGINS": "https://trusted.example"}):
            with self.assertRaises(HTTPException) as raised:
                require_csrf(
                    _request({
                        "Origin": "https://evil.example",
                        "X-CSRF-Token": "correct",
                    }),
                    session,
                )
            self.assertEqual(raised.exception.status_code, 403)
            with self.assertRaises(HTTPException) as raised:
                require_csrf(
                    _request({
                        "Origin": "https://trusted.example",
                        "X-CSRF-Token": "wrong",
                    }),
                    session,
                )
            self.assertEqual(raised.exception.status_code, 403)


if __name__ == "__main__":
    unittest.main()
