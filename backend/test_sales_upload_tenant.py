"""Tenant ownership tests for sales upload preview and confirmation."""

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(__file__))

from auth import (
    AuthenticatedBusinessContext,
    get_authenticated_business_context,
    require_csrf,
)
from main import app
from test_sales_upload import _VALID_ROW_1, _make_csv_bytes


BUSINESS_A = AuthenticatedBusinessContext(
    user_id=1,
    business_id=101,
    role="owner",
    session_id="00000000-0000-4000-8000-000000000001",
)
BUSINESS_B = AuthenticatedBusinessContext(
    user_id=2,
    business_id=202,
    role="owner",
    session_id="00000000-0000-4000-8000-000000000002",
)


class TestSalesUploadTenantIsolation(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        app.dependency_overrides.pop(get_authenticated_business_context, None)
        app.dependency_overrides[require_csrf] = lambda: BUSINESS_A

    def tearDown(self):
        app.dependency_overrides.pop(get_authenticated_business_context, None)
        app.dependency_overrides.pop(require_csrf, None)

    def _use_context(self, context):
        app.dependency_overrides[get_authenticated_business_context] = (
            lambda: context
        )

    def _mock_connection(self):
        connection = MagicMock()
        cursor = MagicMock()
        cursor.fetchall.return_value = []
        connection.cursor.return_value = cursor
        return connection, cursor

    def test_missing_context_is_rejected_before_upload_processing(self):
        with patch("main.parse_and_validate_file") as parser:
            response = self.client.post(
                "/sales/upload-preview",
                files={"file": ("sales.csv", b"not parsed", "text/csv")},
            )

        self.assertEqual(response.status_code, 401)
        parser.assert_not_called()

    @patch("sales_upload.get_db_connection")
    def test_preview_is_bound_to_business_and_other_business_cannot_confirm(
        self,
        get_connection,
    ):
        connection, cursor = self._mock_connection()
        get_connection.return_value = connection
        csv_bytes = _make_csv_bytes([_VALID_ROW_1])

        self._use_context(BUSINESS_A)
        preview_response = self.client.post(
            "/sales/upload-preview",
            files={"file": ("sales.csv", csv_bytes, "text/csv")},
        )
        self.assertEqual(preview_response.status_code, 200)
        preview_id = preview_response.json()["preview_id"]

        self._use_context(BUSINESS_B)
        denied = self.client.post(
            "/sales/import-confirm",
            json={"preview_id": preview_id},
        )
        self.assertEqual(denied.status_code, 400)
        self.assertIn("does not belong", denied.json()["detail"])

        self._use_context(BUSINESS_A)
        confirmed = self.client.post(
            "/sales/import-confirm",
            json={"preview_id": preview_id},
        )
        self.assertEqual(confirmed.status_code, 200)
        inserted_rows = cursor.executemany.call_args.args[1]
        self.assertEqual(inserted_rows[0][0], BUSINESS_A.business_id)

    @patch("sales_upload.get_db_connection")
    def test_confirmation_is_one_time_and_import_rolls_back_on_failure(
        self,
        get_connection,
    ):
        connection, cursor = self._mock_connection()
        cursor.executemany.side_effect = RuntimeError("insert failed")
        get_connection.return_value = connection
        self._use_context(BUSINESS_A)

        preview_response = self.client.post(
            "/sales/upload-preview",
            files={
                "file": (
                    "sales.csv",
                    _make_csv_bytes([_VALID_ROW_1]),
                    "text/csv",
                )
            },
        )
        preview_id = preview_response.json()["preview_id"]
        failed = self.client.post(
            "/sales/import-confirm",
            json={"preview_id": preview_id},
        )
        self.assertEqual(failed.status_code, 500)
        connection.rollback.assert_called_once()

        duplicate = self.client.post(
            "/sales/import-confirm",
            json={"preview_id": preview_id},
        )
        self.assertEqual(duplicate.status_code, 400)

    def test_client_business_id_is_not_authorization_input(self):
        self._use_context(BUSINESS_A)
        with patch("main.answer_question") as unused:
            response = self.client.post(
                "/sales/import-confirm",
                json={
                    "preview_id": "preview-token-123456",
                    "business_id": BUSINESS_B.business_id,
                },
            )

        self.assertEqual(response.status_code, 422)
        unused.assert_not_called()


if __name__ == "__main__":
    unittest.main()
