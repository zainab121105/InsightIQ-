"""Mocked tests for the read-only migration baseline collector."""

import sys
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock

# The repository's existing helper is intentionally not imported by tests.
# This keeps the baseline tests database-free and prevents credential access.
sys.modules.setdefault("database", MagicMock(get_db_connection=MagicMock()))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from migration_baseline import collect_baseline, format_baseline


class TestMigrationBaseline(unittest.TestCase):
    def test_collects_all_select_only_sections_and_closes_cursor(self):
        cursor = MagicMock()
        cursor.fetchone.side_effect = [
            {
                "total_transactions": 2,
                "total_revenue": Decimal("300.00"),
                "total_quantity": 5,
                "average_revenue_per_transaction": Decimal("150.00"),
            },
            {
                "minimum_transaction_id": "TXN0001",
                "maximum_transaction_id": "TXN0002",
                "minimum_order_date": date(2026, 1, 1),
                "maximum_order_date": date(2026, 1, 2),
            },
        ]
        cursor.fetchall.side_effect = [
            [{
                "COLUMN_NAME": "transaction_id",
                "DATA_TYPE": "varchar",
                "COLUMN_TYPE": "varchar(20)",
                "IS_NULLABLE": "NO",
                "COLUMN_KEY": "PRI",
            }],
            [{"product": "Desk", "total_revenue": Decimal("300.00")}],
            [{"region": "North", "total_revenue": Decimal("300.00")}],
            [{"order_month": "2026-01", "total_revenue": Decimal("300.00")}],
            [{"sales_channel": "Online", "total_revenue": Decimal("300.00")}],
        ]
        connection = MagicMock()
        connection.cursor.return_value = cursor

        baseline = collect_baseline(connection)

        self.assertEqual(baseline["summary"]["total_transactions"], 2)
        self.assertEqual(baseline["summary"]["total_revenue"], "300.00")
        self.assertEqual(baseline["transaction_bounds"]["minimum_order_date"], "2026-01-01")
        self.assertEqual(
            baseline["schema"],
            [{
                "column_name": "transaction_id",
                "data_type": "varchar",
                "column_type": "varchar(20)",
                "is_nullable": "NO",
                "column_key": "PRI",
            }],
        )
        self.assertEqual(len(cursor.execute.call_args_list), 7)
        self.assertTrue(all(
            "select" in call.args[0].lower()
            for call in cursor.execute.call_args_list
        ))
        cursor.close.assert_called_once()

    def test_formats_required_sections(self):
        baseline = {
            "summary": {
                "total_transactions": 50,
                "total_revenue": "1484650.00",
                "total_quantity": 652,
                "average_revenue_per_transaction": "29693.00",
            },
            "transaction_bounds": {
                "minimum_transaction_id": "TXN0001",
                "maximum_transaction_id": "TXN1002",
                "minimum_order_date": "2026-01-03",
                "maximum_order_date": "2026-07-05",
            },
            "revenue_by_product": [{"product": "Desk", "total_revenue": 300.0}],
            "revenue_by_region": [{"region": "North", "total_revenue": 300.0}],
            "revenue_by_month": [{"order_month": "2026-01", "total_revenue": 300.0}],
            "revenue_by_sales_channel": [
                {"sales_channel": "Online", "total_revenue": 300.0}
            ],
        }

        output = format_baseline(baseline)

        for expected in (
            "Total transactions: 50",
            "Total revenue: 1484650.00",
            "Revenue by product:",
            "Revenue by region:",
            "Revenue by month:",
            "Revenue by sales channel:",
        ):
            self.assertIn(expected, output)

    def test_formats_schema_columns(self):
            baseline = {
                "summary": {
                    "total_transactions": 50,
                    "total_revenue": "1484650.00",
                    "total_quantity": 652,
                    "average_revenue_per_transaction": "29693.00",
                },
                "transaction_bounds": {
                    "minimum_transaction_id": "TXN0001",
                    "maximum_transaction_id": "TXN1002",
                    "minimum_order_date": "2026-01-03",
                    "maximum_order_date": "2026-07-05",
                },
                "schema": [{
                    "column_name": "transaction_id",
                    "data_type": "varchar",
                    "column_type": "varchar(20)",
                    "is_nullable": "NO",
                    "column_key": "PRI",
                }],
                "revenue_by_product": [],
                "revenue_by_region": [],
                "revenue_by_month": [],
                "revenue_by_sales_channel": [],
            }

            output = format_baseline(baseline)

            self.assertIn(
                "transaction_id: varchar(20) (nullable=NO, key=PRI)",
                output,
            )
            self.assertNotIn("None: None", output)


if __name__ == "__main__":
    unittest.main()
