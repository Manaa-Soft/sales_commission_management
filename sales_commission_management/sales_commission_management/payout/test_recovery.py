from frappe.tests import IntegrationTestCase
import frappe
from frappe import _
from unittest.mock import patch
from types import SimpleNamespace


class TestRecoveryPosition(IntegrationTestCase):
    """Integration tests for the recovery position engine (P3.4.2)."""

    def test_get_commission_recovery_position_missing_entry(self):
        from sales_commission_management.sales_commission_management.payout.recovery import (
            get_commission_recovery_position,
        )
        self.assertRaises(
            frappe.ValidationError,
            get_commission_recovery_position,
            "NONEXISTENT-CE-XYZ",
        )

    def test_get_commission_recovery_position_empty_entry(self):
        from sales_commission_management.sales_commission_management.payout.recovery import (
            get_commission_recovery_position,
        )
        self.assertRaises(
            frappe.ValidationError,
            get_commission_recovery_position,
            "",
        )

    def test_validate_supplier_recovery_link_missing_settlement(self):
        from sales_commission_management.sales_commission_management.payout.recovery import (
            validate_supplier_recovery_link,
        )
        self.assertRaises(
            frappe.ValidationError,
            validate_supplier_recovery_link,
            "NONEXISTENT-SETT",
            "NONEXISTENT-PE",
            "NONEXISTENT-REF",
            "Test Company",
        )

    def test_validate_employee_recovery_link_missing_settlement(self):
        from sales_commission_management.sales_commission_management.payout.recovery import (
            validate_employee_recovery_link,
        )
        self.assertRaises(
            frappe.ValidationError,
            validate_employee_recovery_link,
            "NONEXISTENT-SETT",
            "NONEXISTENT-AS",
        )

    def test_create_recovery_event_missing_data(self):
        from sales_commission_management.sales_commission_management.payout.recovery import (
            create_recovery_event,
        )
        self.assertRaises(frappe.ValidationError, create_recovery_event, None)
        self.assertRaises(frappe.ValidationError, create_recovery_event, {})
        self.assertRaises(
            frappe.ValidationError,
            create_recovery_event,
            {"event_type": "Recovery"},
        )

    def test_create_recovery_event_missing_recovery_of(self):
        from sales_commission_management.sales_commission_management.payout.recovery import (
            create_recovery_event,
        )
        self.assertRaises(
            frappe.ValidationError,
            create_recovery_event,
            {"event_type": "Recovery", "company": "Test"},
        )

    def test_create_recovery_event_nonexistent_payout(self):
        from sales_commission_management.sales_commission_management.payout.recovery import (
            create_recovery_event,
        )
        self.assertRaises(
            frappe.ValidationError,
            create_recovery_event,
            {
                "event_type": "Recovery",
                "recovery_of": "NONEXISTENT-CPE-XYZ",
                "company": "Test Company",
            },
        )

    def test_recovery_gl_receivable_no_mapping(self):
        from sales_commission_management.sales_commission_management.payout.recovery import (
            create_recovery_receivable_gl_entry,
        )
        # No account mapping -> raises
        self.assertRaises(
            frappe.ValidationError,
            create_recovery_receivable_gl_entry,
            "NONEXISTENT-COMPANY",
            "NONEXISTENT-CE",
            100.0,
            "USD",
            "2026-01-01",
        )

    def test_recovery_gl_collection_no_mapping(self):
        from sales_commission_management.sales_commission_management.payout.recovery import (
            create_recovery_collection_gl_entry,
        )
        self.assertRaises(
            frappe.ValidationError,
            create_recovery_collection_gl_entry,
            "NONEXISTENT-COMPANY",
            "NONEXISTENT-CE",
            100.0,
            "USD",
            "2026-01-01",
        )

    def test_recovery_gl_collection_no_bank_account(self):
        from sales_commission_management.sales_commission_management.payout.recovery import (
            create_recovery_collection_gl_entry,
        )
        # No bank account configured -> raises
        self.assertRaises(
            frappe.ValidationError,
            create_recovery_collection_gl_entry,
            "NONEXISTENT-COMPANY",
            "NONEXISTENT-CE",
            100.0,
            "USD",
            "2026-01-01",
            None,
        )

    def test_recovery_gl_receivable_zero_amount(self):
        from sales_commission_management.sales_commission_management.payout.recovery import (
            create_recovery_receivable_gl_entry,
        )
        self.assertRaises(
            frappe.ValidationError,
            create_recovery_receivable_gl_entry,
            "Test Company",
            "CE-001",
            0,
            "USD",
            "2026-01-01",
        )

    def test_recovery_gl_collection_zero_amount(self):
        from sales_commission_management.sales_commission_management.payout.recovery import (
            create_recovery_collection_gl_entry,
        )
        self.assertRaises(
            frappe.ValidationError,
            create_recovery_collection_gl_entry,
            "Test Company",
            "CE-001",
            0,
            "USD",
            "2026-01-01",
        )

    def test_recovery_position_mocked_no_events(self):
        """Test recovery position with a mocked submitted Commission Entry
        that has no payout/recovery/return events."""
        from sales_commission_management.sales_commission_management.payout.recovery import (
            get_commission_recovery_position,
        )

        ce_name = "MOCK-CE-001"
        settlement = None

        # Mock frappe.db.exists
        def mock_exists(dt, name):
            if dt == "Commission Entry" and name == ce_name:
                return True
            return False

        # Mock frappe.db.sql (locking read)
        def mock_sql(query, params, as_dict=False):
            if "FOR UPDATE" in query:
                return [{
                    "name": ce_name,
                    "docstatus": 1,
                    "company": "Test Company",
                    "company_currency": "USD",
                    "commission_currency": "USD",
                    "commission_to_company_rate": 1.0,
                    "earned_commission": 1000.0,
                    "event_type": "Invoice Earned",
                    "original_entry": None,
                    "reverses_entry": None,
                }]
            return []

        # Mock frappe.get_all to return empty lists
        def mock_get_all(dt, filters=None, fields=None, **kwargs):
            return []

        with patch(
            "sales_commission_management.sales_commission_management.payout.recovery.frappe.db.exists",
            side_effect=mock_exists,
        ):
            with patch(
                "sales_commission_management.sales_commission_management.payout.recovery.frappe.db.sql",
                side_effect=mock_sql,
            ):
                with patch(
                    "sales_commission_management.sales_commission_management.payout.recovery.frappe.get_all",
                    side_effect=mock_get_all,
                ):
                    result = get_commission_recovery_position(ce_name)
                    self.assertEqual(result["commission_entry"], ce_name)
                    self.assertEqual(result["currency"], "USD")
                    self.assertEqual(result["original_commission"], 1000.0)
                    self.assertEqual(result["net_entitlement"], 1000.0)
                    self.assertEqual(result["net_paid"], 0.0)
                    self.assertEqual(result["net_recovered"], 0.0)
                    self.assertEqual(result["outstanding_recovery"], 0.0)
                    self.assertEqual(result["payout_allocations"], [])
                    self.assertEqual(result["payout_events"], [])
                    self.assertEqual(result["recovery_events"], [])
                    self.assertEqual(result["return_adjustments"], [])
                    self.assertEqual(result["cancellation_reversals"], [])
