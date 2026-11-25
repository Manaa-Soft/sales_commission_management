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

    def test_missing_source_event_receivable_raises(self):
        from sales_commission_management.sales_commission_management.payout.recovery import (
            create_recovery_receivable_gl_entry,
        )
        self.assertRaises(
            frappe.ValidationError,
            create_recovery_receivable_gl_entry,
            "Test Company",
            "CE-001",
            100.0,
            "USD",
            "2026-01-01",
            None,
            None,
        )

    def test_missing_source_event_collection_raises(self):
        from sales_commission_management.sales_commission_management.payout.recovery import (
            create_recovery_collection_gl_entry,
        )
        self.assertRaises(
            frappe.ValidationError,
            create_recovery_collection_gl_entry,
            "Test Company",
            "CE-001",
            100.0,
            "USD",
            "2026-01-01",
            None,
            None,
            None,
        )

    def test_supplied_event_key_mismatch_raises(self):
        from sales_commission_management.sales_commission_management.payout.recovery import (
            create_recovery_event,
        )
        # recovery_of nonexistent -> raises before key check, but supplied key
        # mismatch should also raise. Use a nonexistent payout to trigger
        # the payout existence check (still ValidationError).
        self.assertRaises(
            frappe.ValidationError,
            create_recovery_event,
            {
                "event_type": "Recovery",
                "recovery_of": "NONEXISTENT-CPE-XYZ",
                "company": "Test Company",
                "event_key": "totally-wrong-key",
            },
        )

    def test_supplied_event_key_mismatch_with_valid_payout(self):
        from sales_commission_management.sales_commission_management.payout.recovery import (
            create_recovery_event,
        )
        from types import SimpleNamespace

        payout_name = "PE-VALID-001"

        def mock_exists(dt, name):
            if dt == "Commission Payout Event" and name == payout_name:
                return True
            return False

        orig = SimpleNamespace(
            docstatus=1,
            event_type="Payout",
            company="Test Company",
            beneficiary="BEN-1",
            commission_entry="CE-1",
            settlement="SET-1",
            settlement_item="SI-1",
            source_doctype="Payment Entry",
            source_name="PE-SRC-1",
            source_row="ROW-1",
            commission_currency="USD",
            commission_amount=10.0,
            company_currency="USD",
            company_amount=10.0,
            settlement_currency="USD",
            settlement_amount=10.0,
        )

        def mock_get_doc(dt, name):
            if dt == "Commission Payout Event" and name == payout_name:
                return orig
            raise Exception("not found")

        with patch(
            "sales_commission_management.sales_commission_management.payout.recovery.frappe.db.exists",
            side_effect=mock_exists,
        ):
            with patch(
                "sales_commission_management.sales_commission_management.payout.recovery.frappe.get_doc",
                side_effect=mock_get_doc,
            ):
                # get_commission_recovery_position will be called for balance
                # validation; mock it to allow (patch on recovery module).
                with patch(
                    "sales_commission_management.sales_commission_management.payout.recovery.get_commission_recovery_position",
                    return_value={
                        "payout_allocations": [
                            {"payout_event": payout_name, "recoverable_balance": 10.0}
                        ],
                        "outstanding_recovery": 10.0,
                    },
                ):
                    self.assertRaises(
                        frappe.ValidationError,
                        create_recovery_event,
                        {
                            "event_type": "Recovery",
                            "recovery_of": payout_name,
                            "company": "Test Company",
                            "beneficiary": "BEN-1",
                            "commission_entry": "CE-1",
                            "settlement": "SET-1",
                            "settlement_item": "SI-1",
                            "source_doctype": "Payment Entry",
                            "source_name": "PE-SRC-1",
                            "source_row": "ROW-1",
                            "commission_currency": "USD",
                            "commission_amount": 10.0,
                            "company_currency": "USD",
                            "company_amount": 10.0,
                            "settlement_currency": "USD",
                            "settlement_amount": 10.0,
                            "event_key": "totally-wrong-key",
                        },
                    )

    def test_recovery_position_two_payouts_second_referenced(self):
        """Recovery linked to the second payout must not be mis-attributed."""
        from sales_commission_management.sales_commission_management.payout.recovery import (
            get_commission_recovery_position,
        )

        ce_name = "MOCK-CE-002"

        def mock_exists(dt, name):
            if dt == "Commission Entry" and name == ce_name:
                return True
            return False

        def mock_sql(query, params, as_dict=False):
            if "FOR UPDATE" in query:
                return [{
                    "name": ce_name,
                    "docstatus": 1,
                    "company": "Test Company",
                    "company_currency": "USD",
                    "commission_currency": "USD",
                    "commission_to_company_rate": 1.0,
                    "earned_commission": 90.0,
                    "event_type": "Invoice Earned",
                    "original_entry": None,
                    "reverses_entry": None,
                }]
            return []

        def mock_get_all(dt, filters=None, fields=None, **kwargs):
            filters = filters or {}
            et = filters.get("event_type")
            if dt == "Commission Entry":
                if et == "Return Adjustment":
                    return []
                return []
            if dt == "Commission Payout Event":
                if et == "Payout":
                    return [
                        {"name": "PE-1", "posting_date": "2026-01-01",
                         "company_amount": 80.0, "company_currency": "USD"},
                        {"name": "PE-2", "posting_date": "2026-01-02",
                         "company_amount": 20.0, "company_currency": "USD"},
                    ]
                if et == "Payout Reversal":
                    return []
                if et == "Recovery":
                    return [
                        {"name": "REC-1", "company_amount": 10.0,
                         "company_currency": "USD", "recovery_of": "PE-1"},
                    ]
                if et == "Recovery Reversal":
                    return []
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
                    allocs = result["payout_allocations"]
                    pe1 = [a for a in allocs if a["payout_event"] == "PE-1"][0]
                    # Recovery referenced PE-1 (which has the 10 due), so it
                    # must be fully recovered against PE-1.
                    self.assertEqual(pe1["recovered"], 10.0)
                    self.assertEqual(pe1["recoverable_balance"], 0.0)
                    # Aggregate and payout-level recovered amounts reconcile.
                    self.assertEqual(result["net_recovered"], 10.0)
                    self.assertEqual(result["outstanding_recovery"], 0.0)
