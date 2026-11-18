from frappe.tests import IntegrationTestCase

from sales_commission_management.sales_commission_management.doctype.commission_settlement.commission_settlement import (
    _available_balance_for_entry,
    _reserved_for_entry,
)


class TestCommissionSettlement(IntegrationTestCase):
    def test_helpers_importable(self):
        self.assertTrue(callable(_available_balance_for_entry))
        self.assertTrue(callable(_reserved_for_entry))
