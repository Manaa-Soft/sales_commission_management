from frappe.tests import IntegrationTestCase
import math

import frappe
from frappe.utils import flt

from sales_commission_management.sales_commission_management.payout.supplier_payment import (
    get_supplier_settlement_for_reference,
)
from sales_commission_management.sales_commission_management.payout.supplier_payment import (
    allocate_supplier_payment_across_items,
)


class TestSupplierPaymentAllocation(IntegrationTestCase):
    def test_proportional_allocation(self):
        items = [
            {"item": "A", "settlement_amount": 100, "previously_paid": 0},
            {"item": "B", "settlement_amount": 200, "previously_paid": 0},
        ]
        res = allocate_supplier_payment_across_items(
            payment_allocation=90,
            source_allocation=90,
            target_exchange_rate=1,
            settlement_fx_rate=1,
            items=items,
        )
        # 1/3 and 2/3
        self.assertEqual(len(res), 2)
        total = sum(r["allocation"] for r in res)
        self.assertAlmostEqual(total, 90, places=2)

    def test_reject_over_allocation(self):
        items = [{"item": "A", "settlement_amount": 50, "previously_paid": 0}]
        try:
            allocate_supplier_payment_across_items(
                payment_allocation=60,
                source_allocation=60,
                target_exchange_rate=1,
                settlement_fx_rate=1,
                items=items,
            )
            self.fail("Expected ValueError")
        except ValueError:
            pass

    def test_deterministic_rounding(self):
        items = [
            {"item": "A", "settlement_amount": 100, "previously_paid": 0},
            {"item": "B", "settlement_amount": 100, "previously_paid": 0},
            {"item": "C", "settlement_amount": 100, "previously_paid": 0},
        ]
        r1 = allocate_supplier_payment_across_items(
            payment_allocation=100,
            source_allocation=100,
            target_exchange_rate=1,
            settlement_fx_rate=1,
            items=items,
        )
        r2 = allocate_supplier_payment_across_items(
            payment_allocation=100,
            source_allocation=100,
            target_exchange_rate=1,
            settlement_fx_rate=1,
            items=items,
        )
        self.assertEqual(r1, r2)

    def test_explicit_target_rate_zero_rejected(self):
        items = [{"item": "A", "settlement_amount": 100, "previously_paid": 0}]
        with self.assertRaises(ValueError):
            allocate_supplier_payment_across_items(
                payment_allocation=10,
                source_allocation=10,
                target_exchange_rate=0,
                settlement_fx_rate=1,
                items=items,
            )

    def test_explicit_settlement_fx_zero_rejected(self):
        items = [{"item": "A", "settlement_amount": 100, "previously_paid": 0}]
        with self.assertRaises(ValueError):
            allocate_supplier_payment_across_items(
                payment_allocation=10,
                source_allocation=10,
                target_exchange_rate=1,
                settlement_fx_rate=0,
                items=items,
            )

    def test_missing_exchange_rates_rejected(self):
        items = [{"item": "A", "settlement_amount": 100, "previously_paid": 0}]
        with self.assertRaises(ValueError):
            allocate_supplier_payment_across_items(
                payment_allocation=10,
                source_allocation=10,
                items=items,
            )

    def test_multi_currency_minor_unit_reconciles(self):
        items = [
            {"item": "A", "settlement_amount": 0.02, "previously_paid": 0},
            {"item": "B", "settlement_amount": 0.01, "previously_paid": 0},
        ]
        res = allocate_supplier_payment_across_items(
            payment_allocation=0.01,
            source_allocation=0.02,
            target_exchange_rate=1,
            settlement_fx_rate=1,
            items=items,
        )
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["item"], "A")
        self.assertAlmostEqual(res[0]["settlement_amount"], 0.01, places=2)
        self.assertAlmostEqual(res[0]["source_amount"], 0.02, places=2)
        total_sett = sum(r["settlement_amount"] for r in res)
        total_src = sum(r["source_amount"] for r in res)
        self.assertAlmostEqual(total_sett, 0.01, places=2)
        self.assertAlmostEqual(total_src, 0.02, places=2)

    def test_zero_share_settlement_no_lost_allocations(self):
        items = [
            {"item": "A", "settlement_amount": 0.02, "previously_paid": 0},
            {"item": "B", "settlement_amount": 0.01, "previously_paid": 0},
        ]
        res = allocate_supplier_payment_across_items(
            payment_allocation=0.01,
            source_allocation=0.02,
            target_exchange_rate=1,
            settlement_fx_rate=1,
            items=items,
        )
        # Item B receives no settlement allocation and is excluded, but the
        # source total is still fully allocated to eligible items.
        self.assertEqual(len(res), 1)
        total_src = sum(r["source_amount"] for r in res)
        self.assertAlmostEqual(total_src, 0.02, places=2)

    def test_three_equal_items_minor_distribution(self):
        items = [
            {"item": "A", "settlement_amount": 100, "previously_paid": 0},
            {"item": "B", "settlement_amount": 100, "previously_paid": 0},
            {"item": "C", "settlement_amount": 100, "previously_paid": 0},
        ]
        res = allocate_supplier_payment_across_items(
            payment_allocation=100,
            source_allocation=100,
            target_exchange_rate=1,
            settlement_fx_rate=1,
            items=items,
        )
        allocs = sorted(r["allocation"] for r in res)
        # 10000 minor / 3 = 3333.333; largest remainder gives 33.34,33.33,33.33
        self.assertEqual(allocs, [33.33, 33.33, 33.34])

    def test_reference_list_empty_rejected(self):
        pe = {
            "name": "PE-1",
            "docstatus": 1,
            "payment_type": "Pay",
            "party_type": "Supplier",
            "company": "C",
            "party": "S",
            "references": [],
        }
        pr = {
            "parent": "PE-1",
            "reference_doctype": "Purchase Invoice",
            "reference_name": "PI-1",
            "allocated_amount": 10,
        }
        self.assertIsNone(get_supplier_settlement_for_reference(pe, pr))

    def test_matching_child_name_altered_fields_rejected(self):
        pe = {
            "name": "PE-1",
            "docstatus": 1,
            "payment_type": "Pay",
            "party_type": "Supplier",
            "company": "C",
            "party": "S",
            "references": [
                {
                    "name": "REF-1",
                    "reference_doctype": "Purchase Invoice",
                    "reference_name": "PI-1",
                    "allocated_amount": 10,
                }
            ],
        }
        pr = {
            "name": "REF-1",
            "parent": "PE-1",
            "reference_doctype": "Purchase Invoice",
            "reference_name": "PI-1",
            "allocated_amount": 999,
        }
        self.assertIsNone(get_supplier_settlement_for_reference(pe, pr))

    def test_reference_from_another_payment_entry_rejected(self):
        pe = {
            "name": "PE-1",
            "docstatus": 1,
            "payment_type": "Pay",
            "party_type": "Supplier",
            "company": "C",
            "party": "S",
            "references": [],
        }
        pr = {
            "parent": "OTHER-PE",
            "reference_doctype": "Purchase Invoice",
            "reference_name": "PI-1",
            "allocated_amount": 10,
        }
        self.assertIsNone(get_supplier_settlement_for_reference(pe, pr))

    def test_amount_included_converted_through_fx_snapshot(self):
        items = [{"item": "A", "amount_included": 100, "previously_paid": 0}]
        res = allocate_supplier_payment_across_items(
            payment_allocation=200,
            source_allocation=200,
            target_exchange_rate=1,
            settlement_fx_rate=2,
            items=items,
        )
        self.assertEqual(len(res), 1)
        self.assertAlmostEqual(res[0]["settlement_amount"], 200, places=2)
        self.assertAlmostEqual(res[0]["commission_amount"], 100, places=2)

    def test_previous_partial_payment_allocates_outstanding(self):
        items = [{"item": "A", "settlement_amount": 100, "previously_paid": 40}]
        res = allocate_supplier_payment_across_items(
            payment_allocation=60,
            source_allocation=60,
            target_exchange_rate=1,
            settlement_fx_rate=1,
            items=items,
        )
        self.assertEqual(len(res), 1)
        self.assertAlmostEqual(res[0]["allocation"], 60, places=2)

    def test_zero_commission_allocation_rejected(self):
        # Three items each 0.01 commission (comm currency), FX rate 3 so settlement
        # 0.03 each. Payment 0.03 settlement -> commission total 0.01, which
        # cannot be split across three items at 2-decimal precision.
        items = [
            {"item": "A", "settlement_amount": 0.01, "previously_paid": 0},
            {"item": "B", "settlement_amount": 0.01, "previously_paid": 0},
            {"item": "C", "settlement_amount": 0.01, "previously_paid": 0},
        ]
        with self.assertRaises(ValueError):
            allocate_supplier_payment_across_items(
                payment_allocation=0.03,
                source_allocation=0.03,
                target_exchange_rate=1,
                settlement_fx_rate=3,
                items=items,
            )

    def test_negative_nan_infinite_rejected(self):
        items = [{"item": "A", "settlement_amount": 100, "previously_paid": 0}]
        for bad in (-1, float("nan"), float("inf"), float("-inf")):
            with self.assertRaises(ValueError):
                allocate_supplier_payment_across_items(
                    payment_allocation=bad if bad != -1 else -5,
                    source_allocation=10,
                    target_exchange_rate=1,
                    settlement_fx_rate=1,
                    items=items,
                )
        with self.assertRaises(ValueError):
            allocate_supplier_payment_across_items(
                payment_allocation=10,
                source_allocation=10,
                target_exchange_rate=-1,
                settlement_fx_rate=1,
                items=items,
            )
        with self.assertRaises(ValueError):
            allocate_supplier_payment_across_items(
                payment_allocation=10,
                source_allocation=10,
                target_exchange_rate=1,
                settlement_fx_rate=float("nan"),
                items=items,
            )