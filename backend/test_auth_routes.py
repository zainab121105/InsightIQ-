"""Database-free tests for the authentication HTTP route layer."""

import os
import sys
import unittest
from datetime import datetime
from types import ModuleType
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(__file__))
database_stub = ModuleType("database")
database_stub.get_db_connection = MagicMock()
sys.modules.setdefault("database", database_stub)

from fastapi import HTTPException, Response  # noqa: E402
from starlette.requests import Request  # noqa: E402

from auth import (  # noqa: E402
    AuthenticatedSession,
    AuthenticationError,
    AuthenticationResult,
    AuthService,
    LoginRequest,
    SignupRequest,
    _cookie_options,
    auth_router,
    businesses,
    csrf_bootstrap,
    get_authenticated_session,
    login,
    logout,
    me,
    refresh,
    signup,
)


SESSION_ID = "00000000-0000-4000-8000-000000000001"


def _request(headers=None, cookies=None):
    raw_headers = [
        (key.lower().encode("ascii"), value.encode("ascii"))
        for key, value in (headers or {}).items()
    ]
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/auth/test",
        "query_string": b"",
        "headers": raw_headers,
        "cookies": cookies or {},
    }
    request = Request(scope)
    if cookies:
        cookie_header = "; ".join(f"{key}={value}" for key, value in cookies.items())
        request = Request(
            {
                **scope,
                "headers": raw_headers
                + [(b"cookie", cookie_header.encode("ascii"))],
            }
        )
    return request


def _result():
    return AuthenticationResult(
        session=AuthenticatedSession(
            user_id=7,
            session_id=SESSION_ID,
            access_token_jti="00000000-0000-4000-8000-000000000002",
            csrf_token_hash=b"hash",
            expires_at=datetime(2026, 12, 1),
        ),
        access_token="access-value",
        refresh_token="refresh-value",
        csrf_token="csrf-value",
    )


class TestAuthRoutes(unittest.TestCase):
    def test_login_sets_httponly_cookies_without_refresh_json(self):
        service = MagicMock()
        service.login.return_value = _result()
        response = Response()

        body = login(LoginRequest(email="user@example.com", password="password"), response, service)

        self.assertEqual(body["user_id"], 7)
        self.assertNotIn("refresh_token", body)
        set_cookie = b"\n".join(
            value for key, value in response.raw_headers if key == b"set-cookie"
        ).decode().lower()
        self.assertIn("httponly", set_cookie)
        self.assertIn("insightiq_access=", set_cookie)
        self.assertIn("insightiq_refresh=", set_cookie)
        self.assertIn("insightiq_refresh=refresh-value; httponly; max-age=2592000; path=/auth;", set_cookie)

    @patch.dict(os.environ, {"APP_ENV": "production"}, clear=True)
    def test_production_requires_secure_cookie_configuration(self):
        with self.assertRaisesRegex(RuntimeError, "must be configured"):
            _cookie_options("/")

        with patch.dict(os.environ, {"AUTH_COOKIE_SECURE": "false"}, clear=False):
            with self.assertRaisesRegex(RuntimeError, "enabled in production"):
                _cookie_options("/")

    def test_login_returns_generic_authentication_error(self):
        service = MagicMock()
        service.login.side_effect = AuthenticationError("invalid credentials")

        with self.assertRaisesRegex(HTTPException, "authentication failed"):
            login(
                LoginRequest(email="user@example.com", password="wrong"),
                Response(),
                service,
            )

    def test_signup_sets_cookies_and_returns_business_id(self):
        service = MagicMock()
        service.signup.return_value = MagicMock(authentication=_result(), business_id=11)

        body = signup(
            SignupRequest(
                email="owner@example.com",
                password="password",
                display_name="Owner",
                business_name="Acme",
                business_slug="acme",
            ),
            Response(),
            service,
        )

        self.assertEqual(body["business_id"], 11)
        self.assertNotIn("refresh_token", body)
        service.signup.assert_called_once()

    @patch.dict(os.environ, {"AUTH_ALLOWED_ORIGINS": "https://app.example.test"}, clear=False)
    def test_refresh_requires_origin_and_csrf(self):
        service = MagicMock()
        service.validate_refresh_csrf.return_value = True
        service.refresh.return_value = _result()
        request = _request(
            headers={
                "Origin": "https://app.example.test",
                "X-CSRF-Token": "csrf-value",
            },
            cookies={"insightiq_refresh": "refresh-value"},
        )

        body = refresh(request, Response(), service)

        self.assertEqual(body["csrf_token"], "csrf-value")
        service.refresh.assert_called_once_with("refresh-value")

        with self.assertRaises(HTTPException) as raised:
            refresh(
                _request(
                    headers={"Origin": "https://evil.example.test"},
                    cookies={"insightiq_refresh": "refresh-value"},
                ),
                Response(),
                service,
            )
        self.assertEqual(raised.exception.status_code, 403)

    @patch.dict(os.environ, {"AUTH_ALLOWED_ORIGINS": "https://app.example.test"}, clear=False)
    def test_csrf_bootstrap_rotates_without_existing_csrf_header(self):
        service = MagicMock()
        service.rotate_csrf_token.return_value = "fresh-csrf"
        response = Response()

        body = csrf_bootstrap(
            response,
            _request(headers={"Origin": "https://app.example.test"}),
            _result().session,
            service,
        )

        self.assertEqual(body, {"csrf_token": "fresh-csrf"})
        self.assertEqual(response.headers["cache-control"], "no-store")
        service.rotate_csrf_token.assert_called_once_with(SESSION_ID, 7)

    def test_csrf_bootstrap_rejects_disallowed_origin(self):
        with self.assertRaisesRegex(HTTPException, "CSRF validation failed"):
            csrf_bootstrap(
                Response(),
                _request(headers={"Origin": "https://evil.example.test"}),
                _result().session,
                MagicMock(),
            )

    def test_csrf_bootstrap_rejects_missing_origin(self):
        with self.assertRaisesRegex(HTTPException, "CSRF validation failed"):
            csrf_bootstrap(
                Response(),
                _request(),
                _result().session,
                MagicMock(),
            )

    def test_csrf_bootstrap_requires_authenticated_session(self):
        with self.assertRaisesRegex(HTTPException, "authentication required"):
            get_authenticated_session(_request(), MagicMock())

    def test_logout_uses_csrf_authenticated_session(self):
        service = MagicMock()
        response = Response()
        session = _result().session

        body = logout(response, session, service)

        self.assertEqual(body["status"], "logged_out")
        service.logout.assert_called_once_with(SESSION_ID, 7)
        self.assertIn("expires=", response.headers["set-cookie"].lower())

    def test_me_and_businesses_use_authenticated_user(self):
        service = MagicMock()
        service.get_user_profile.return_value = {
            "user_id": 7,
            "email": "user@example.com",
            "display_name": "User",
            "status": "active",
        }
        service.list_businesses.return_value = [
            {"business_id": 11, "name": "Acme", "slug": "acme", "role": "owner"}
        ]
        session = _result().session

        profile = me(session, service)
        business_list = businesses(session, service)

        self.assertEqual(profile["user"]["user_id"], 7)
        self.assertEqual(business_list[0]["role"], "owner")
        service.list_businesses.assert_called_once_with(7)


class TestAuthRouterRegistration(unittest.TestCase):
    def test_required_paths_are_registered(self):
        paths = {route.path for route in auth_router.routes}
        self.assertEqual(
            paths,
            {
                "/auth/signup",
                "/auth/login",
                "/auth/refresh",
                "/auth/csrf",
                "/auth/logout",
                "/auth/me",
                "/auth/businesses",
            },
        )
