import frappe
from frappe.tests import IntegrationTestCase

from sales_commission_management.sales_commission_management.doctype.commission_entry.commission_entry import (
    _calculate_basis,
    _match_rule,
    _resolve_plan,
    _event_key,
    _item_group_path,
)


class TestCommissionEngine(IntegrationTestCase):
    def test_item_group_path(self):
        # basic smoke: function returns a list
        result = _item_group_path(None)
        self.assertIsInstance(result, list)

    def test_event_key_uniqueness(self):
        k1 = _event_key("Sales Invoice", "SI-001", "row1", "PLAN-1", "INTERNAL", "Sales Person", "SP-1", "Invoice Earned")
        k2 = _event_key("Sales Invoice", "SI-001", "row1", "PLAN-1", "INTERNAL", "Sales Person", "SP-1", "Invoice Earned")
        self.assertEqual(k1, k2)
        k3 = _event_key("Sales Invoice", "SI-001", "row1", "PLAN-1", "INTERNAL", "Sales Person", "SP-1", "Return Adjustment")
        self.assertNotEqual(k1, k3)