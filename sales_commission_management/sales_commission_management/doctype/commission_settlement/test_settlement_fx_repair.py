from frappe.tests import IntegrationTestCase
import frappe
from frappe import _

from sales_commission_management.sales_commission_management.doctype.commission_settlement.commission_settlement import (
    repair_settlement_fx,
)


class TestSettlementFxRepair(IntegrationTestCase):
    def test_repair_function_importable(self):
        self.assertTrue(callable(repair_settlement_fx))

    def test_repair_requires_settlement(self):
        self.assertRaises(frappe.ValidationError, repair_settlement_fx, None)

    def test_repair_missing_settlement(self):
        self.assertRaises(frappe.ValidationError, repair_settlement_fx, "NONEXISTENT-CS-XYZ")
