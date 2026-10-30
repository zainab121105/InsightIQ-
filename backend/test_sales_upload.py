"""Automated tests for Sales Data Upload, Validation, Staging, and Batch Confirmation.

All database interactions are mocked to guarantee zero modifications to live MySQL data.

Test classes:
- TestSalesUploadValidation   : original validation rules
- TestSalesImportConfirmation : staging, token lifecycle, atomic commit
- TestSecurityHardening       : new protections added in the hardening pass
"""

from __future__ import annotations

import io
import time
import unittest
import zipfile
from unittest.mock import MagicMock, patch

import openpyxl
import pandas as pd
from fastapi.testclient import TestClient

from main import app
import sales_upload


# ---------------------------------------------------------------------------
# CSV / XLSX helper builders
# ---------------------------------------------------------------------------

def _make_csv_bytes(rows: list[dict], columns: list[str] | None = None) -> bytes:
    df = pd.DataFrame(rows)
    if columns:
        df = df[columns]
    output = io.StringIO()
    df.to_csv(output, index=False)
    return output.getvalue().encode("utf-8")


def _make_xlsx_bytes(rows: list[dict], columns: list[str] | None = None) -> bytes:
    df = pd.DataFrame(rows)
    if columns:
        df = df[columns]
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df.to_excel(writer, index=False)
    return output.getvalue()


# ---------------------------------------------------------------------------
# Shared test fixtures
# ---------------------------------------------------------------------------

_VALID_ROW_1 = {
    "Transaction_Id": "TXN9901",
    "Order_date": "2026-07-01",
    "Product": "Wireless Mouse",
    "Category": "Accessories",
    "Region": "South",
    "Sales_channel": "Retail",
    "Quantity": "10",
    "Unit_price": "650",
    "Discount_amount": "0",
    "Status": "Completed",
}

_VALID_ROW_2 = {
    "Transaction_Id": "TXN9902",
    "Order_date": "05-07-2026",  # DD-MM-YYYY format
    "Product": "Office Chair",
    "Category": "Furniture",
    "Region": "North",
    "Sales_channel": "Online",
    "Quantity": "2",
    "Unit_price": "7800",
    "Discount_amount": "600",
    "Status": "Completed",
}


# ---------------------------------------------------------------------------
# Original validation tests (must all continue to pass)
# ---------------------------------------------------------------------------

class TestSalesUploadValidation(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    @patch("sales_upload.get_db_connection")
    def test_valid_csv_preview(self, mock_db_conn):
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchall.return_value = []
        mock_conn.cursor.return_value = mock_cur
        mock_db_conn.return_value = mock_conn

        csv_bytes = _make_csv_bytes([_VALID_ROW_1, _VALID_ROW_2])
        response = self.client.post(
            "/sales/upload-preview",
            files={"file": ("test_sales.csv", csv_bytes, "text/csv")},
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("preview_id", data)
        self.assertEqual(data["summary"]["total_rows"], 2)
        self.assertEqual(data["summary"]["valid_rows_count"], 2)
        self.assertEqual(data["summary"]["invalid_rows_count"], 0)
        # Row 1: 10 * 650 - 0 = 6500.0
        # Row 2: 2 * 7800 - 600 = 15000.0
        # Total: 21500.0
        self.assertEqual(data["summary"]["projected_revenue"], 21500.0)
        self.assertEqual(data["summary"]["projected_quantity"], 12)
        self.assertEqual(len(data["valid_records_preview"]), 2)
        self.assertEqual(data["valid_records_preview"][0]["revenue"], 6500.0)
        self.assertEqual(data["valid_records_preview"][0]["order_month"], "2026-07")
        self.assertEqual(data["valid_records_preview"][1]["order_month"], "2026-07")

    @patch("sales_upload.get_db_connection")
    def test_valid_xlsx_preview(self, mock_db_conn):
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchall.return_value = []
        mock_conn.cursor.return_value = mock_cur
        mock_db_conn.return_value = mock_conn

        xlsx_bytes = _make_xlsx_bytes([_VALID_ROW_1, _VALID_ROW_2])
        response = self.client.post(
            "/sales/upload-preview",
            files={
                "file": (
                    "test_sales.xlsx",
                    xlsx_bytes,
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
            },
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["summary"]["valid_rows_count"], 2)
        self.assertEqual(data["summary"]["projected_revenue"], 21500.0)

    @patch("sales_upload.get_db_connection")
    def test_missing_columns(self, mock_db_conn):
        # Missing 'Quantity' column
        bad_row = dict(_VALID_ROW_1)
        del bad_row["Quantity"]
        csv_bytes = _make_csv_bytes([bad_row])

        response = self.client.post(
            "/sales/upload-preview",
            files={"file": ("bad_columns.csv", csv_bytes, "text/csv")},
        )
        self.assertEqual(response.status_code, 422)
        self.assertIn("missing required column", response.json()["detail"].lower())

    @patch("sales_upload.get_db_connection")
    def test_invalid_dates(self, mock_db_conn):
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchall.return_value = []
        mock_conn.cursor.return_value = mock_cur
        mock_db_conn.return_value = mock_conn

        bad_date_row = dict(_VALID_ROW_1, Order_date="32-13-2026")
        csv_bytes = _make_csv_bytes([bad_date_row])

        response = self.client.post(
            "/sales/upload-preview",
            files={"file": ("bad_date.csv", csv_bytes, "text/csv")},
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["summary"]["invalid_rows_count"], 1)
        reasons = data["rejected_records"][0]["rejection_reasons"]
        self.assertTrue(any("Invalid order date" in r for r in reasons))

    @patch("sales_upload.get_db_connection")
    def test_invalid_quantities_and_prices(self, mock_db_conn):
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchall.return_value = []
        mock_conn.cursor.return_value = mock_cur
        mock_db_conn.return_value = mock_conn

        bad_qty = dict(_VALID_ROW_1, Transaction_Id="TXN_Q0", Quantity="0")
        bad_price = dict(_VALID_ROW_1, Transaction_Id="TXN_P0", Unit_price="-50")
        csv_bytes = _make_csv_bytes([bad_qty, bad_price])

        response = self.client.post(
            "/sales/upload-preview",
            files={"file": ("bad_num.csv", csv_bytes, "text/csv")},
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["summary"]["invalid_rows_count"], 2)
        reasons_0 = data["rejected_records"][0]["rejection_reasons"]
        reasons_1 = data["rejected_records"][1]["rejection_reasons"]
        self.assertTrue(any("Quantity must be a positive integer" in r for r in reasons_0))
        self.assertTrue(any("Unit price must be greater than zero" in r for r in reasons_1))

    @patch("sales_upload.get_db_connection")
    def test_invalid_discounts(self, mock_db_conn):
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchall.return_value = []
        mock_conn.cursor.return_value = mock_cur
        mock_db_conn.return_value = mock_conn

        # Negative discount
        neg_disc = dict(_VALID_ROW_1, Transaction_Id="TXN_ND", Discount_amount="-10")
        # Discount > Quantity * Unit_price (10 * 650 = 6500, discount = 7000)
        excess_disc = dict(
            _VALID_ROW_1,
            Transaction_Id="TXN_ED",
            Quantity="10",
            Unit_price="650",
            Discount_amount="7000",
        )
        csv_bytes = _make_csv_bytes([neg_disc, excess_disc])

        response = self.client.post(
            "/sales/upload-preview",
            files={"file": ("bad_disc.csv", csv_bytes, "text/csv")},
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["summary"]["invalid_rows_count"], 2)
        r0 = data["rejected_records"][0]["rejection_reasons"]
        r1 = data["rejected_records"][1]["rejection_reasons"]
        self.assertTrue(any("cannot be negative" in r for r in r0))
        self.assertTrue(any("exceeds gross amount" in r for r in r1))

    @patch("sales_upload.get_db_connection")
    def test_non_completed_transactions(self, mock_db_conn):
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchall.return_value = []
        mock_conn.cursor.return_value = mock_cur
        mock_db_conn.return_value = mock_conn

        ret_row = dict(_VALID_ROW_1, Transaction_Id="TXN_RET", Status="Returned")
        can_row = dict(_VALID_ROW_1, Transaction_Id="TXN_CAN", Status="Cancelled")
        csv_bytes = _make_csv_bytes([ret_row, can_row])

        response = self.client.post(
            "/sales/upload-preview",
            files={"file": ("non_completed.csv", csv_bytes, "text/csv")},
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["summary"]["invalid_rows_count"], 2)
        for rej in data["rejected_records"]:
            self.assertIn("Transaction not completed", rej["rejection_reasons"])

    @patch("sales_upload.get_db_connection")
    def test_duplicate_ids_within_file(self, mock_db_conn):
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchall.return_value = []
        mock_conn.cursor.return_value = mock_cur
        mock_db_conn.return_value = mock_conn

        dup_row = dict(_VALID_ROW_1, Transaction_Id="TXN_SAME")
        dup_row_2 = dict(_VALID_ROW_2, Transaction_Id="TXN_SAME")
        csv_bytes = _make_csv_bytes([dup_row, dup_row_2])

        response = self.client.post(
            "/sales/upload-preview",
            files={"file": ("in_file_dups.csv", csv_bytes, "text/csv")},
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["summary"]["valid_rows_count"], 1)
        self.assertEqual(data["summary"]["invalid_rows_count"], 1)
        self.assertEqual(data["summary"]["in_file_duplicates_count"], 1)
        self.assertIn(
            "Duplicate Transaction ID within uploaded file",
            data["rejected_records"][0]["rejection_reasons"],
        )

    @patch("sales_upload.get_db_connection")
    def test_existing_database_transaction_ids(self, mock_db_conn):
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        # Mock database returning TXN0001 as already present
        mock_cur.fetchall.return_value = [{"transaction_id": "TXN0001"}]
        mock_conn.cursor.return_value = mock_cur
        mock_db_conn.return_value = mock_conn

        collision_row = dict(_VALID_ROW_1, Transaction_Id="TXN0001")
        new_row = dict(_VALID_ROW_2, Transaction_Id="TXN_NEW")
        csv_bytes = _make_csv_bytes([collision_row, new_row])

        response = self.client.post(
            "/sales/upload-preview",
            files={"file": ("db_dup.csv", csv_bytes, "text/csv")},
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["summary"]["valid_rows_count"], 1)
        self.assertEqual(data["summary"]["existing_db_duplicates_count"], 1)
        rej = data["rejected_records"][0]
        self.assertEqual(rej["transaction_id"], "TXN0001")
        self.assertIn("Transaction ID already exists in database", rej["rejection_reasons"])

    def test_incorrect_file_extensions(self):
        csv_bytes = _make_csv_bytes([_VALID_ROW_1])
        response = self.client.post(
            "/sales/upload-preview",
            files={"file": ("test.json", csv_bytes, "application/json")},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("Only .csv and .xlsx files are supported", response.json()["detail"])

    def test_oversized_files(self):
        # Create bytes > 10MB
        oversized = b"a" * (10 * 1024 * 1024 + 1024)
        response = self.client.post(
            "/sales/upload-preview",
            files={"file": ("huge.csv", oversized, "text/csv")},
        )
        self.assertEqual(response.status_code, 413)
        self.assertIn("exceeds maximum limit", response.json()["detail"])

    @patch("sales_upload.get_db_connection")
    def test_server_side_revenue_recalculation_ignores_supplied_revenue(self, mock_db_conn):
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchall.return_value = []
        mock_conn.cursor.return_value = mock_cur
        mock_db_conn.return_value = mock_conn

        # Upload provides a false Revenue=999999
        row_with_tampered_revenue = dict(
            _VALID_ROW_1,
            Quantity="5",
            Unit_price="1000",
            Discount_amount="200",
            Revenue="999999.0",
        )
        csv_bytes = _make_csv_bytes([row_with_tampered_revenue])

        response = self.client.post(
            "/sales/upload-preview",
            files={"file": ("tampered_rev.csv", csv_bytes, "text/csv")},
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        # Calculated: 5 * 1000 - 200 = 4800.0 (NOT 999999.0)
        self.assertEqual(data["valid_records_preview"][0]["revenue"], 4800.0)
        self.assertEqual(data["summary"]["projected_revenue"], 4800.0)


# ---------------------------------------------------------------------------
# Original import-confirmation tests (must all continue to pass)
# ---------------------------------------------------------------------------

class TestSalesImportConfirmation(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_unknown_or_tampered_preview_identifier(self):
        response = self.client.post(
            "/sales/import-confirm",
            json={"preview_id": "non_existent_token_1234567890abcdef"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("Invalid, expired, or already confirmed", response.json()["detail"])

    @patch("sales_upload.get_db_connection")
    def test_preview_expiration(self, mock_db_conn):
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchall.return_value = []
        mock_conn.cursor.return_value = mock_cur
        mock_db_conn.return_value = mock_conn

        csv_bytes = _make_csv_bytes([_VALID_ROW_1])
        preview_resp = self.client.post(
            "/sales/upload-preview",
            files={"file": ("exp.csv", csv_bytes, "text/csv")},
        )
        preview_id = preview_resp.json()["preview_id"]

        # Manually backdate expiration in staging store
        with sales_upload._STAGING_LOCK:
            if preview_id in sales_upload._STAGING_STORE:
                sales_upload._STAGING_STORE[preview_id]["expires_at"] = time.time() - 10

        response = self.client.post(
            "/sales/import-confirm",
            json={"preview_id": preview_id},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("Invalid, expired, or already confirmed", response.json()["detail"])

    @patch("sales_upload.get_db_connection")
    def test_successful_batch_insertion(self, mock_db_conn):
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        # First preview check: no existing IDs
        # Confirm import pre-check: no existing IDs
        mock_cur.fetchall.return_value = []
        mock_conn.cursor.return_value = mock_cur
        mock_db_conn.return_value = mock_conn

        csv_bytes = _make_csv_bytes([_VALID_ROW_1, _VALID_ROW_2])
        preview_resp = self.client.post(
            "/sales/upload-preview",
            files={"file": ("batch.csv", csv_bytes, "text/csv")},
        )
        self.assertEqual(preview_resp.status_code, 200)
        preview_id = preview_resp.json()["preview_id"]

        confirm_resp = self.client.post(
            "/sales/import-confirm",
            json={"preview_id": preview_id},
        )
        self.assertEqual(confirm_resp.status_code, 200)
        data = confirm_resp.json()
        self.assertEqual(data["status"], "success")
        self.assertEqual(data["imported_count"], 2)
        self.assertEqual(data["imported_revenue"], 21500.0)

        # Verify SQL commit called and executemany called
        mock_cur.executemany.assert_called_once()
        mock_conn.commit.assert_called_once()
        mock_conn.rollback.assert_not_called()

        # Verify preview token was invalidated (cannot be re-used)
        reuse_resp = self.client.post(
            "/sales/import-confirm",
            json={"preview_id": preview_id},
        )
        self.assertEqual(reuse_resp.status_code, 400)

    @patch("sales_upload.get_db_connection")
    def test_rollback_when_insertion_fails(self, mock_db_conn):
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchall.return_value = []
        # Force executemany to raise database error
        mock_cur.executemany.side_effect = RuntimeError("Deadlock / Connection lost")
        mock_conn.cursor.return_value = mock_cur
        mock_db_conn.return_value = mock_conn

        csv_bytes = _make_csv_bytes([_VALID_ROW_1])
        preview_resp = self.client.post(
            "/sales/upload-preview",
            files={"file": ("rollback.csv", csv_bytes, "text/csv")},
        )
        preview_id = preview_resp.json()["preview_id"]

        confirm_resp = self.client.post(
            "/sales/import-confirm",
            json={"preview_id": preview_id},
        )
        self.assertEqual(confirm_resp.status_code, 500)
        self.assertIn("rolled back", confirm_resp.json()["detail"].lower())
        mock_conn.rollback.assert_called_once()
        mock_conn.commit.assert_not_called()

    @patch("sales_upload.get_db_connection")
    def test_no_overwriting_of_recently_committed_records_during_confirm(self, mock_db_conn):
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        # Preview stage: no collisions
        mock_cur.fetchall.side_effect = [
            [],  # for preview
            [{"transaction_id": _VALID_ROW_1["Transaction_Id"]}],  # for confirm pre-check
        ]
        mock_conn.cursor.return_value = mock_cur
        mock_db_conn.return_value = mock_conn

        csv_bytes = _make_csv_bytes([_VALID_ROW_1])
        preview_resp = self.client.post(
            "/sales/upload-preview",
            files={"file": ("race.csv", csv_bytes, "text/csv")},
        )
        preview_id = preview_resp.json()["preview_id"]

        confirm_resp = self.client.post(
            "/sales/import-confirm",
            json={"preview_id": preview_id},
        )
        self.assertEqual(confirm_resp.status_code, 400)
        self.assertIn("recently committed", confirm_resp.json()["detail"])
        mock_cur.executemany.assert_not_called()
        mock_conn.commit.assert_not_called()


# ---------------------------------------------------------------------------
# NEW: Security hardening tests
# ---------------------------------------------------------------------------

class TestSecurityHardening(unittest.TestCase):
    """Tests for protections added in the security hardening pass."""

    def setUp(self):
        self.client = TestClient(app)

    # ------------------------------------------------------------------
    # 1. File size enforcement — streaming read aborts at 10 MB
    # ------------------------------------------------------------------

    def test_streaming_upload_exceeds_10mb_returns_413(self):
        """Verify that a payload just over 10 MB is rejected with HTTP 413."""
        oversized = b"x" * (sales_upload.MAX_FILE_SIZE_BYTES + 1)
        response = self.client.post(
            "/sales/upload-preview",
            files={"file": ("large.csv", oversized, "text/csv")},
        )
        self.assertEqual(response.status_code, 413)
        self.assertIn("exceeds maximum limit", response.json()["detail"])

    def test_file_exactly_at_limit_is_rejected_as_malformed_not_413(self):
        """A payload of exactly MAX_FILE_SIZE_BYTES passes the size gate but is
        rejected later as an invalid CSV — confirming the limit is inclusive."""
        at_limit = b"a" * sales_upload.MAX_FILE_SIZE_BYTES
        response = self.client.post(
            "/sales/upload-preview",
            files={"file": ("exact.csv", at_limit, "text/csv")},
        )
        # Should NOT be 413; may be 200 (all-rejected) or 422 (missing headers)
        self.assertNotEqual(response.status_code, 413)

    # ------------------------------------------------------------------
    # 2. XLSX ZIP inspection — expansion ratio, entry count
    # ------------------------------------------------------------------

    def _make_zip_bomb_xlsx(self) -> bytes:
        """Create a minimal ZIP archive whose uncompressed/compressed ratio
        exceeds MAX_XLSX_EXPANSION_RATIO.

        Strategy: build a real XLSX, then inflate its declared uncompressed
        sizes in the central directory so the ratio check fires without
        actually decompressing a huge payload.

        Because Python's zipfile module validates sizes on extraction but NOT
        on metadata read, we can craft the metadata to appear suspicious
        without actually storing gigabytes.
        """
        # Build a legitimate small XLSX first
        real_xlsx = _make_xlsx_bytes([_VALID_ROW_1])

        # Re-write the central directory, inflating all file_size entries by
        # a factor that exceeds MAX_XLSX_EXPANSION_RATIO * compressed_size
        buf = io.BytesIO(real_xlsx)
        with zipfile.ZipFile(buf, "r") as zf_in:
            entries = zf_in.infolist()
            data_map = {}
            for entry in entries:
                data_map[entry.filename] = zf_in.read(entry.filename)

        out = io.BytesIO()
        with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as zf_out:
            for entry in entries:
                raw = data_map[entry.filename]
                info = zipfile.ZipInfo(entry.filename)
                # Inflate the declared uncompressed size in the metadata
                # so the ratio check triggers
                zf_out.writestr(info, raw)
                # After writing, manipulate the last added member's file_size
                last = zf_out.filelist[-1]
                # Set file_size so ratio > MAX_XLSX_EXPANSION_RATIO
                last.file_size = last.compress_size * (sales_upload.MAX_XLSX_EXPANSION_RATIO + 10)

        return out.getvalue()

    def test_xlsx_zip_with_excessive_expansion_ratio_is_rejected(self):
        """XLSX files whose ZIP expansion ratio exceeds the limit are rejected."""
        crafted = self._make_zip_bomb_xlsx()
        response = self.client.post(
            "/sales/upload-preview",
            files={
                "file": (
                    "bomb.xlsx",
                    crafted,
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
            },
        )
        # Either 422 (ValueError from _inspect_xlsx_zip) or passes through to
        # openpyxl parse (which may also fail on corrupt metadata). The key
        # assertion is that it does NOT return 200 with valid records.
        self.assertNotEqual(response.status_code, 200)

    def test_xlsx_with_too_many_zip_entries_is_rejected(self):
        """XLSX archives with entry count > MAX_XLSX_ZIP_ENTRIES are rejected."""
        # Build a ZIP that has too many entries but still starts with PK magic bytes
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            for i in range(sales_upload.MAX_XLSX_ZIP_ENTRIES + 1):
                zf.writestr(f"entry_{i:05d}.xml", f"<data>{i}</data>")
        crafted_bytes = buf.getvalue()

        # Call parse_and_validate_file directly (bypasses route-level extension check)
        with self.assertRaises(ValueError) as ctx:
            sales_upload.parse_and_validate_file(crafted_bytes, "many_entries.xlsx")
        self.assertIn("entries", str(ctx.exception).lower())

    def test_xlsx_with_excessive_uncompressed_size_is_rejected(self):
        """XLSX archives whose total uncompressed size > MAX_XLSX_UNCOMPRESSED_BYTES are rejected."""
        # Build a ZIP where the declared uncompressed size is inflated
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            small_content = b"A" * 1024  # 1 KB actual data
            zf.writestr("xl/worksheets/sheet1.xml", small_content)
            # Override the declared file_size on the written entry
            last = zf.filelist[-1]
            last.file_size = sales_upload.MAX_XLSX_UNCOMPRESSED_BYTES + 1
        crafted_bytes = buf.getvalue()

        with self.assertRaises(ValueError) as ctx:
            sales_upload.parse_and_validate_file(crafted_bytes, "oversized.xlsx")
        self.assertIn("expand", str(ctx.exception).lower())

    def test_valid_xlsx_passes_zip_inspection(self):
        """Legitimate XLSX files must pass ZIP inspection without errors."""
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchall.return_value = []
        mock_conn.cursor.return_value = mock_cur

        with patch("sales_upload.get_db_connection", return_value=mock_conn):
            xlsx_bytes = _make_xlsx_bytes([_VALID_ROW_1, _VALID_ROW_2])
            response = self.client.post(
                "/sales/upload-preview",
                files={
                    "file": (
                        "legit.xlsx",
                        xlsx_bytes,
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    )
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["summary"]["valid_rows_count"], 2)

    # ------------------------------------------------------------------
    # 3. Maximum row count enforcement
    # ------------------------------------------------------------------

    def test_csv_exceeding_max_rows_is_rejected(self):
        """CSV files with more than MAX_ROWS data rows return HTTP 422."""
        # Build MAX_ROWS + 1 valid-looking rows
        rows = [
            dict(
                _VALID_ROW_1,
                Transaction_Id=f"TXNBIG{i:05d}",
            )
            for i in range(sales_upload.MAX_ROWS + 1)
        ]
        csv_bytes = _make_csv_bytes(rows)

        # This file is large; skip the route (which enforces 10 MB) and call
        # parse_and_validate_file directly to isolate the row-count check.
        # We also need to mock the DB call that would happen for duplicate checking.
        with patch("sales_upload.get_db_connection") as mock_db_conn:
            mock_conn = MagicMock()
            mock_cur = MagicMock()
            mock_cur.fetchall.return_value = []
            mock_conn.cursor.return_value = mock_cur
            mock_db_conn.return_value = mock_conn

            with self.assertRaises(ValueError) as ctx:
                sales_upload.parse_and_validate_file(csv_bytes, "too_many_rows.csv")
        err_msg = str(ctx.exception).lower()
        self.assertIn("exceeds", err_msg)
        # The error message formats the limit with locale commas ("5,000"), so
        # check for the presence of that formatted string.
        formatted_limit = f"{sales_upload.MAX_ROWS:,}"
        self.assertIn(formatted_limit, str(ctx.exception))

    def test_xlsx_exceeding_max_rows_is_rejected(self):
        """XLSX files with more than MAX_ROWS data rows return HTTP 422."""
        rows = [
            dict(
                _VALID_ROW_1,
                Transaction_Id=f"TXNBIG{i:05d}",
            )
            for i in range(sales_upload.MAX_ROWS + 1)
        ]
        xlsx_bytes = _make_xlsx_bytes(rows)

        with patch("sales_upload.get_db_connection") as mock_db_conn:
            mock_conn = MagicMock()
            mock_cur = MagicMock()
            mock_cur.fetchall.return_value = []
            mock_conn.cursor.return_value = mock_cur
            mock_db_conn.return_value = mock_conn

            with self.assertRaises(ValueError) as ctx:
                sales_upload.parse_and_validate_file(xlsx_bytes, "too_many_rows.xlsx")
        self.assertIn("exceeds", str(ctx.exception).lower())

    @patch("sales_upload.get_db_connection")
    def test_file_at_max_row_limit_is_accepted(self, mock_db_conn):
        """A file with exactly MAX_ROWS rows should not be rejected by the row cap."""
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchall.return_value = []
        mock_conn.cursor.return_value = mock_cur
        mock_db_conn.return_value = mock_conn

        rows = [
            dict(_VALID_ROW_1, Transaction_Id=f"TXNOK{i:05d}")
            for i in range(sales_upload.MAX_ROWS)
        ]
        csv_bytes = _make_csv_bytes(rows)
        # Directly call parse_and_validate_file — avoids the 10 MB route limit
        result = sales_upload.parse_and_validate_file(csv_bytes, "at_limit.csv")
        self.assertEqual(result["summary"]["total_rows"], sales_upload.MAX_ROWS)

    # ------------------------------------------------------------------
    # 4. Text field length limits
    # ------------------------------------------------------------------

    @patch("sales_upload.get_db_connection")
    def test_oversized_transaction_id_is_rejected(self, mock_db_conn):
        """Transaction IDs longer than the maximum are row-rejected."""
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchall.return_value = []
        mock_conn.cursor.return_value = mock_cur
        mock_db_conn.return_value = mock_conn

        max_len = sales_upload.TEXT_FIELD_MAX_LENGTHS["transaction_id"]
        oversized_id = "X" * (max_len + 1)
        bad_row = dict(_VALID_ROW_1, Transaction_Id=oversized_id)
        csv_bytes = _make_csv_bytes([bad_row])

        response = self.client.post(
            "/sales/upload-preview",
            files={"file": ("long_id.csv", csv_bytes, "text/csv")},
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["summary"]["invalid_rows_count"], 1)
        reasons = data["rejected_records"][0]["rejection_reasons"]
        self.assertTrue(any("transaction_id" in r and "maximum length" in r for r in reasons))

    @patch("sales_upload.get_db_connection")
    def test_oversized_product_name_is_rejected(self, mock_db_conn):
        """Product names longer than the maximum are row-rejected."""
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchall.return_value = []
        mock_conn.cursor.return_value = mock_cur
        mock_db_conn.return_value = mock_conn

        max_len = sales_upload.TEXT_FIELD_MAX_LENGTHS["product"]
        bad_row = dict(_VALID_ROW_1, Transaction_Id="TXN_LONGPROD", Product="P" * (max_len + 1))
        csv_bytes = _make_csv_bytes([bad_row])

        response = self.client.post(
            "/sales/upload-preview",
            files={"file": ("long_product.csv", csv_bytes, "text/csv")},
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["summary"]["invalid_rows_count"], 1)
        reasons = data["rejected_records"][0]["rejection_reasons"]
        self.assertTrue(any("product" in r and "maximum length" in r for r in reasons))

    @patch("sales_upload.get_db_connection")
    def test_oversized_region_is_rejected(self, mock_db_conn):
        """Region values longer than the maximum are row-rejected."""
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchall.return_value = []
        mock_conn.cursor.return_value = mock_cur
        mock_db_conn.return_value = mock_conn

        max_len = sales_upload.TEXT_FIELD_MAX_LENGTHS["region"]
        bad_row = dict(_VALID_ROW_1, Transaction_Id="TXN_LONGREG", Region="R" * (max_len + 1))
        csv_bytes = _make_csv_bytes([bad_row])

        response = self.client.post(
            "/sales/upload-preview",
            files={"file": ("long_region.csv", csv_bytes, "text/csv")},
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["summary"]["invalid_rows_count"], 1)
        reasons = data["rejected_records"][0]["rejection_reasons"]
        self.assertTrue(any("region" in r and "maximum length" in r for r in reasons))

    @patch("sales_upload.get_db_connection")
    def test_text_at_max_length_is_accepted(self, mock_db_conn):
        """Values exactly at the maximum length must pass the length check."""
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchall.return_value = []
        mock_conn.cursor.return_value = mock_cur
        mock_db_conn.return_value = mock_conn

        max_id_len = sales_upload.TEXT_FIELD_MAX_LENGTHS["transaction_id"]
        row = dict(_VALID_ROW_1, Transaction_Id="T" * max_id_len)
        csv_bytes = _make_csv_bytes([row])

        response = self.client.post(
            "/sales/upload-preview",
            files={"file": ("at_max_len.csv", csv_bytes, "text/csv")},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["summary"]["valid_rows_count"], 1)

    @patch("sales_upload.get_db_connection")
    def test_text_fields_not_silently_truncated(self, mock_db_conn):
        """Oversized values must be REJECTED not silently truncated.
        If a valid record is returned, its field must be the original string,
        not a truncated version.
        """
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchall.return_value = []
        mock_conn.cursor.return_value = mock_cur
        mock_db_conn.return_value = mock_conn

        max_len = sales_upload.TEXT_FIELD_MAX_LENGTHS["product"]
        long_product = "P" * (max_len + 5)
        bad_row = dict(_VALID_ROW_1, Transaction_Id="TXN_TRUNC", Product=long_product)
        csv_bytes = _make_csv_bytes([bad_row])

        response = self.client.post(
            "/sales/upload-preview",
            files={"file": ("truncation_test.csv", csv_bytes, "text/csv")},
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        # Row must be rejected — it should NOT appear in valid_records_preview
        self.assertEqual(data["summary"]["valid_rows_count"], 0)
        self.assertEqual(data["summary"]["invalid_rows_count"], 1)
        # If it somehow appeared in valid records, ensure value is NOT truncated
        for rec in data.get("valid_records_preview", []):
            self.assertNotEqual(rec["product"], long_product[:max_len],
                                "Field was silently truncated instead of rejected")

    # ------------------------------------------------------------------
    # 5. Legitimate text values are preserved (no formula stripping)
    # ------------------------------------------------------------------

    @patch("sales_upload.get_db_connection")
    def test_product_name_starting_with_dash_is_preserved(self, mock_db_conn):
        """Product names that start with '-' (e.g. '-Angle Bracket') are
        preserved as-is; the code must NOT strip leading special characters."""
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchall.return_value = []
        mock_conn.cursor.return_value = mock_cur
        mock_db_conn.return_value = mock_conn

        row = dict(_VALID_ROW_1, Transaction_Id="TXN_DASH", Product="-Angle Bracket")
        csv_bytes = _make_csv_bytes([row])

        response = self.client.post(
            "/sales/upload-preview",
            files={"file": ("dash_product.csv", csv_bytes, "text/csv")},
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["summary"]["valid_rows_count"], 1)
        preview = data["valid_records_preview"][0]
        self.assertEqual(preview["product"], "-Angle Bracket",
                         "Leading '-' was stripped from product name")

    @patch("sales_upload.get_db_connection")
    def test_product_name_starting_with_plus_is_preserved(self, mock_db_conn):
        """Product names starting with '+' are passed through unchanged."""
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchall.return_value = []
        mock_conn.cursor.return_value = mock_cur
        mock_db_conn.return_value = mock_conn

        row = dict(_VALID_ROW_1, Transaction_Id="TXN_PLUS", Product="+Adapter Cable")
        csv_bytes = _make_csv_bytes([row])

        response = self.client.post(
            "/sales/upload-preview",
            files={"file": ("plus_product.csv", csv_bytes, "text/csv")},
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["summary"]["valid_rows_count"], 1)
        self.assertEqual(data["valid_records_preview"][0]["product"], "+Adapter Cable")



    # ------------------------------------------------------------------
    # 6. transaction_id length boundary (VARCHAR(20) database column)
    # ------------------------------------------------------------------

    @patch("sales_upload.get_db_connection")
    def test_transaction_id_at_max_length_is_accepted(self, mock_db_conn):
        """A transaction ID of exactly 20 characters must be accepted.

        The MySQL column is VARCHAR(20), so a 20-character ID is the
        boundary value and must pass the length check.
        """
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchall.return_value = []
        mock_conn.cursor.return_value = mock_cur
        mock_db_conn.return_value = mock_conn

        max_len = sales_upload.TEXT_FIELD_MAX_LENGTHS["transaction_id"]
        self.assertEqual(max_len, 20, "Expected transaction_id limit to be 20")

        row = dict(_VALID_ROW_1, Transaction_Id="T" * max_len)
        csv_bytes = _make_csv_bytes([row])

        response = self.client.post(
            "/sales/upload-preview",
            files={"file": ("txn_at_max.csv", csv_bytes, "text/csv")},
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(
            data["summary"]["valid_rows_count"], 1,
            f"A {max_len}-char transaction ID must not be rejected",
        )

    @patch("sales_upload.get_db_connection")
    def test_transaction_id_one_over_max_is_rejected(self, mock_db_conn):
        """A transaction ID of 21 characters must be rejected with a clear
        row-specific message naming the field and the limit.

        21 chars would exceed the VARCHAR(20) MySQL column and must never
        reach the database.
        """
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchall.return_value = []
        mock_conn.cursor.return_value = mock_cur
        mock_db_conn.return_value = mock_conn

        max_len = sales_upload.TEXT_FIELD_MAX_LENGTHS["transaction_id"]
        self.assertEqual(max_len, 20, "Expected transaction_id limit to be 20")

        oversized_id = "T" * (max_len + 1)  # 21 characters
        row = dict(_VALID_ROW_1, Transaction_Id=oversized_id)
        csv_bytes = _make_csv_bytes([row])

        response = self.client.post(
            "/sales/upload-preview",
            files={"file": ("txn_over_max.csv", csv_bytes, "text/csv")},
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(
            data["summary"]["invalid_rows_count"], 1,
            "A 21-char transaction ID must be rejected",
        )
        self.assertEqual(data["summary"]["valid_rows_count"], 0)
        reasons = data["rejected_records"][0]["rejection_reasons"]
        self.assertTrue(
            any("transaction_id" in r and "maximum length" in r for r in reasons),
            f"Rejection reason must name 'transaction_id' and 'maximum length'; got: {reasons}",
        )

    # ------------------------------------------------------------------
    # 7. Text field length boundaries (VARCHAR(50) / VARCHAR(30) columns)
    # ------------------------------------------------------------------

    @patch("sales_upload.get_db_connection")
    def test_region_at_max_length_is_accepted(self, mock_db_conn):
        """A region of exactly 50 characters must be accepted."""
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchall.return_value = []
        mock_conn.cursor.return_value = mock_cur
        mock_db_conn.return_value = mock_conn

        max_len = sales_upload.TEXT_FIELD_MAX_LENGTHS["region"]
        self.assertEqual(max_len, 50, "Expected region limit to be 50")

        row = dict(_VALID_ROW_1, Transaction_Id="TXN_REG50", Region="R" * max_len)
        csv_bytes = _make_csv_bytes([row])

        response = self.client.post(
            "/sales/upload-preview",
            files={"file": ("region_at_max.csv", csv_bytes, "text/csv")},
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(
            data["summary"]["valid_rows_count"], 1,
            f"A {max_len}-char region must be accepted",
        )
        self.assertEqual(data["summary"]["invalid_rows_count"], 0)

    @patch("sales_upload.get_db_connection")
    def test_region_one_over_max_is_rejected(self, mock_db_conn):
        """A region of 51 characters must be rejected during preview validation
        with a message identifying 'region' and the 50-character limit.
        """
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchall.return_value = []
        mock_conn.cursor.return_value = mock_cur
        mock_db_conn.return_value = mock_conn

        max_len = sales_upload.TEXT_FIELD_MAX_LENGTHS["region"]
        self.assertEqual(max_len, 50, "Expected region limit to be 50")

        oversized = "R" * (max_len + 1)  # 51 characters
        row = dict(_VALID_ROW_1, Transaction_Id="TXN_REG51", Region=oversized)
        csv_bytes = _make_csv_bytes([row])

        response = self.client.post(
            "/sales/upload-preview",
            files={"file": ("region_over_max.csv", csv_bytes, "text/csv")},
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["summary"]["invalid_rows_count"], 1)
        self.assertEqual(data["summary"]["valid_rows_count"], 0)
        reasons = data["rejected_records"][0]["rejection_reasons"]
        self.assertTrue(
            any("region" in r and "maximum length of 50" in r for r in reasons),
            f"Rejection reason must name 'region' and 'maximum length of 50'; got: {reasons}",
        )

    @patch("sales_upload.get_db_connection")
    def test_sales_channel_at_max_length_is_accepted(self, mock_db_conn):
        """A sales channel of exactly 30 characters must be accepted."""
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchall.return_value = []
        mock_conn.cursor.return_value = mock_cur
        mock_db_conn.return_value = mock_conn

        max_len = sales_upload.TEXT_FIELD_MAX_LENGTHS["sales_channel"]
        self.assertEqual(max_len, 30, "Expected sales_channel limit to be 30")

        row = dict(_VALID_ROW_1, Transaction_Id="TXN_CHAN30", Sales_channel="C" * max_len)
        csv_bytes = _make_csv_bytes([row])

        response = self.client.post(
            "/sales/upload-preview",
            files={"file": ("chan_at_max.csv", csv_bytes, "text/csv")},
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(
            data["summary"]["valid_rows_count"], 1,
            f"A {max_len}-char sales_channel must be accepted",
        )
        self.assertEqual(data["summary"]["invalid_rows_count"], 0)

    @patch("sales_upload.get_db_connection")
    def test_sales_channel_one_over_max_is_rejected(self, mock_db_conn):
        """A sales channel of 31 characters must be rejected during preview validation
        with a message identifying 'sales_channel' and the 30-character limit.
        """
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchall.return_value = []
        mock_conn.cursor.return_value = mock_cur
        mock_db_conn.return_value = mock_conn

        max_len = sales_upload.TEXT_FIELD_MAX_LENGTHS["sales_channel"]
        self.assertEqual(max_len, 30, "Expected sales_channel limit to be 30")

        oversized = "C" * (max_len + 1)  # 31 characters
        row = dict(_VALID_ROW_1, Transaction_Id="TXN_CHAN31", Sales_channel=oversized)
        csv_bytes = _make_csv_bytes([row])

        response = self.client.post(
            "/sales/upload-preview",
            files={"file": ("chan_over_max.csv", csv_bytes, "text/csv")},
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["summary"]["invalid_rows_count"], 1)
        self.assertEqual(data["summary"]["valid_rows_count"], 0)
        reasons = data["rejected_records"][0]["rejection_reasons"]
        self.assertTrue(
            any("sales_channel" in r and "maximum length of 30" in r for r in reasons),
            f"Rejection reason must name 'sales_channel' and 'maximum length of 30'; got: {reasons}",
        )

    @patch("sales_upload.get_db_connection")
    def test_status_at_max_length_is_accepted(self, mock_db_conn):
        """A status of exactly 30 characters must pass the length validation check."""
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchall.return_value = []
        mock_conn.cursor.return_value = mock_cur
        mock_db_conn.return_value = mock_conn

        max_len = sales_upload.TEXT_FIELD_MAX_LENGTHS["status"]
        self.assertEqual(max_len, 30, "Expected status limit to be 30")

        # 30-character status string
        row = dict(_VALID_ROW_1, Transaction_Id="TXN_STAT30", Status="S" * max_len)
        csv_bytes = _make_csv_bytes([row])

        response = self.client.post(
            "/sales/upload-preview",
            files={"file": ("status_at_max.csv", csv_bytes, "text/csv")},
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        reasons = data["rejected_records"][0]["rejection_reasons"]
        # Must pass length validation (no status length rejection reason)
        self.assertFalse(
            any("status" in r and "maximum length" in r for r in reasons),
            f"A {max_len}-char status must not trigger length violation; got: {reasons}",
        )

    @patch("sales_upload.get_db_connection")
    def test_status_one_over_max_is_rejected(self, mock_db_conn):
        """A status of 31 characters must be rejected during preview validation
        with a message identifying 'status' and the 30-character limit.
        """
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchall.return_value = []
        mock_conn.cursor.return_value = mock_cur
        mock_db_conn.return_value = mock_conn

        max_len = sales_upload.TEXT_FIELD_MAX_LENGTHS["status"]
        self.assertEqual(max_len, 30, "Expected status limit to be 30")

        oversized = "S" * (max_len + 1)  # 31 characters
        row = dict(_VALID_ROW_1, Transaction_Id="TXN_STAT31", Status=oversized)
        csv_bytes = _make_csv_bytes([row])

        response = self.client.post(
            "/sales/upload-preview",
            files={"file": ("status_over_max.csv", csv_bytes, "text/csv")},
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["summary"]["invalid_rows_count"], 1)
        self.assertEqual(data["summary"]["valid_rows_count"], 0)
        reasons = data["rejected_records"][0]["rejection_reasons"]
        self.assertTrue(
            any("status" in r and "maximum length of 30" in r for r in reasons),
            f"Rejection reason must name 'status' and 'maximum length of 30'; got: {reasons}",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
