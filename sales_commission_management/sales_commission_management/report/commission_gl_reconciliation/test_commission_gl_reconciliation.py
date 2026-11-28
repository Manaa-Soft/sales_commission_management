from frappe.tests import IntegrationTestCase
import frappe


class TestCommissionGlReconciliation(IntegrationTestCase):
    def test_company_required(self):
        from sales_commission_management.sales_commission_management.report.commission_gl_reconciliation.commission_gl_reconciliation import get_data

        self.assertRaises(frappe.ValidationError, get_data, {})

    def test_missing_company_filter_raises(self):
        from sales_commission_management.sales_commission_management.report.commission_gl_reconciliation.commission_gl_reconciliation import get_data

        try:
            get_data({"from_date": "2026-01-01", "to_date": "2026-12-31"})
        except frappe.ValidationError:
            return
        self.fail("Expected ValidationError when company is missing")