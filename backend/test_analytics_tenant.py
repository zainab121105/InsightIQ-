"""Database-free tenant isolation tests for the core analytics queries."""

import unittest
import os
import sys
from decimal import Decimal
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(__file__))
from auth import AuthenticatedBusinessContext
import analytics


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


class TenantAwareConnection:
    def __init__(self):
        self.cursor_instance = MagicMock()
        self.cursor_instance.execute.side_effect = self.execute
        self.last_business_id = None

    def cursor(self, **_kwargs):
        return self.cursor_instance

    def close(self):
        return None

    def execute(self, query, parameters=()):
        self.last_business_id = parameters[0]
        self.cursor_instance.fetchone.return_value = self.summary_row()
        self.cursor_instance.fetchall.return_value = self.rows(query)

    def summary_row(self):
        if self.last_business_id == 101:
            return {
                "total_transactions": 1,
                "total_quantity": 2,
                "total_revenue": Decimal("20.00"),
                "average_revenue": Decimal("20.00"),
            }
        return {
            "total_transactions": 1,
            "total_quantity": 5,
            "total_revenue": Decimal("50.00"),
            "average_revenue": Decimal("50.00"),
        }

    def rows(self, query):
        if "product" in query:
            key = "product"
            value = "A Product" if self.last_business_id == 101 else "B Product"
        elif "region" in query:
            key = "region"
            value = "A Region" if self.last_business_id == 101 else "B Region"
        else:
            key = "order_month"
            value = "2026-01" if self.last_business_id == 101 else "2026-02"
        return [{key: value, "total_revenue": Decimal("20.00" if self.last_business_id == 101 else "50.00")}]


class TestAnalyticsTenantIsolation(unittest.TestCase):
    def _run_for_both(self, function):
        connections = [TenantAwareConnection(), TenantAwareConnection()]

        with patch(
            "analytics.get_db_connection",
            side_effect=connections,
        ):
            first = function(BUSINESS_A)
            second = function(BUSINESS_B)

        self.assertNotEqual(first, second)
        self.assertEqual(connections[0].last_business_id, BUSINESS_A.business_id)
        self.assertEqual(connections[1].last_business_id, BUSINESS_B.business_id)

    def test_summary_is_scoped_to_business(self):
        self._run_for_both(analytics.get_sales_summary)

    def test_products_are_scoped_to_business(self):
        self._run_for_both(analytics.get_product_revenue)

    def test_regions_are_scoped_to_business(self):
        self._run_for_both(analytics.get_region_revenue)

    def test_months_are_scoped_to_business(self):
        self._run_for_both(analytics.get_monthly_revenue)

    def test_missing_context_is_rejected_before_database_access(self):
        with patch("analytics.get_db_connection") as get_connection:
            with self.assertRaises(ValueError):
                analytics.get_sales_summary(None)  # type: ignore[arg-type]
            get_connection.assert_not_called()

    def test_chat_rejects_missing_context_before_database_access(self):
        import chat

        with patch("chat._chat_completion") as completion:
            with self.assertRaisesRegex(
                RuntimeError,
                "Authenticated business context is required",
            ):
                chat.answer_question("Give me an overall business summary.")
        completion.assert_not_called()

    def test_chat_dispatches_all_tools_with_server_context(self):
        import chat

        calls = []
        functions = {}
        for name in chat.ANALYTICS_FUNCTIONS:
            def tool(*, context, _name=name, **arguments):
                calls.append((_name, context, arguments))
                return []

            tool.__module__ = "analytics"
            functions[name] = tool

        context_token = chat._active_business_context.set(BUSINESS_A)
        try:
            with patch.dict(chat.ANALYTICS_FUNCTIONS, functions, clear=True):
                for name in functions:
                    chat._call_analytics(name, {"region": "Business A"})
        finally:
            chat._active_business_context.reset(context_token)

        self.assertEqual({name for name, _, _ in calls}, set(functions))
        self.assertTrue(all(context is BUSINESS_A for _, context, _ in calls))
        self.assertTrue(all(
            "business_id" not in arguments
            and "user_id" not in arguments
            and "role" not in arguments
            for _, _, arguments in calls
        ))
