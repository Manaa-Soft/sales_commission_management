from frappe.tests import IntegrationTestCase
import frappe
from frappe.utils import flt

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