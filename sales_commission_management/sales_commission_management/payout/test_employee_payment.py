from frappe.tests import IntegrationTestCase
import frappe


class TestEmployeePayment(IntegrationTestCase):
    """Integration tests for employee payroll payout detection and validation."""

    def test_get_employee_settlement_for_additional_salary(self):
        from sales_commission_management.sales_commission_management.payout.employee_payment import (
            get_employee_settlement_for_additional_salary,
        )
        # No Additional Salary -> None
        result = get_employee_settlement_for_additional_salary("NONEXISTENT-AS")
        self.assertIsNone(result)

    def test_get_payroll_evidence_level(self):
        from sales_commission_management.sales_commission_management.payout.employee_payment import (
            get_payroll_evidence_level,
        )
        self.assertEqual(get_payroll_evidence_level(), "Payroll Accrued")
        self.assertEqual(get_payroll_evidence_level(salary_slip="SS-001"), "Payroll Accrued")
        self.assertEqual(get_payroll_evidence_level(payroll_entry="PE-001"), "Payroll Bank Entry")
        self.assertEqual(get_payroll_evidence_level(bank_entry="BE-001"), "Bank Reconciled")

    def test_validate_employee_payroll_link_mismatch(self):
        from sales_commission_management.sales_commission_management.payout.employee_payment import (
            validate_employee_payroll_link,
        )
        self.assertRaises(frappe.ValidationError, validate_employee_payroll_link, "NONEXISTENT-SETT", "NONEXISTENT-AS")