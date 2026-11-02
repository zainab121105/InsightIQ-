"""API-level tests for authenticated chat tenant propagation."""

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))

from fastapi.testclient import TestClient

from auth import (
    AuthenticatedBusinessContext,
    get_authenticated_business_context,
    require_csrf,
)
from main import app


BUSINESS_A = AuthenticatedBusinessContext(
    user_id=1,
    business_id=101,
    role="owner",
    session_id="00000000-0000-4000-8000-000000000001",
)
BUSINESS_B = AuthenticatedBusinessContext(
    user_id=2,
    business_id=202,
    role="member",
    session_id="00000000-0000-4000-8000-000000000002",
)


class TestChatRoutes(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        app.dependency_overrides.pop(get_authenticated_business_context, None)
        app.dependency_overrides[require_csrf] = lambda: BUSINESS_A

    def tearDown(self):
        app.dependency_overrides.pop(get_authenticated_business_context, None)
        app.dependency_overrides.pop(require_csrf, None)

    def test_unauthenticated_chat_is_rejected_before_chat_service(self):
        with patch("main.answer_question") as answer_question:
            response = self.client.post(
                "/chat",
                json={"question": "Give me a summary."},
            )

        self.assertEqual(response.status_code, 401)
        answer_question.assert_not_called()

    def test_authenticated_business_context_is_passed_to_chat(self):
        app.dependency_overrides[get_authenticated_business_context] = (
            lambda: BUSINESS_A
        )
        with patch(
            "main.answer_question",
            return_value=(
                "Business A answer",
                {"planning_ms": 0.0, "database_ms": 0.0, "response_generation_ms": 0.0, "total_ms": 0.0},
                {"business_id": BUSINESS_A.business_id},
            ),
        ) as answer_question:
            response = self.client.post(
                "/chat",
                json={"question": "Give me a summary."},
            )

        self.assertEqual(response.status_code, 200)
        self.assertIs(
            answer_question.call_args.kwargs["business_context"],
            BUSINESS_A,
        )

    def test_business_b_cannot_use_business_a_context(self):
        app.dependency_overrides[get_authenticated_business_context] = (
            lambda: BUSINESS_B
        )
        with patch(
            "main.answer_question",
            return_value=(
                "Business B answer",
                {"planning_ms": 0.0, "database_ms": 0.0, "response_generation_ms": 0.0, "total_ms": 0.0},
                {"business_id": BUSINESS_B.business_id},
            ),
        ) as answer_question:
            response = self.client.post(
                "/chat",
                json={"question": "Show Business A revenue."},
            )

        self.assertEqual(response.status_code, 200)
        context = answer_question.call_args.kwargs["business_context"]
        self.assertIs(context, BUSINESS_B)
        self.assertNotEqual(context.business_id, BUSINESS_A.business_id)
        self.assertEqual(response.json()["business_data"]["business_id"], 202)

    def test_client_business_id_cannot_override_authenticated_context(self):
        app.dependency_overrides[get_authenticated_business_context] = (
            lambda: BUSINESS_A
        )
        with patch("main.answer_question") as answer_question:
            response = self.client.post(
                "/chat",
                json={
                    "question": "Give me a summary.",
                    "business_id": BUSINESS_B.business_id,
                },
            )

        self.assertEqual(response.status_code, 422)
        answer_question.assert_not_called()


if __name__ == "__main__":
    unittest.main()
