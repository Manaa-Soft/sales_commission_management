from frappe.tests import IntegrationTestCase
import frappe


class TestEmployeePayment(IntegrationTestCase):
    """Integration tests for employee payroll payout detection and validation."""

    def test_get_payroll_evidence_level_no_evidence(self):
        from sales_commission_management.sales_commission_management.payout.employee_payment import (
            get_payroll_evidence_level,
        )
        # No evidence -> None
        self.assertIsNone(get_payroll_evidence_level())

    def test_get_payroll_evidence_level_submitted_salary_slip(self):
        from sales_commission_management.sales_commission_management.payout.employee_payment import (
            get_payroll_evidence_level,
        )
        # Without a real submitted Salary Slip, returns None
        self.assertIsNone(get_payroll_evidence_level(salary_slip="NONEXISTENT-SS"))

    def test_get_payroll_evidence_level_bank_entry_requires_payroll_entry(self):
        from sales_commission_management.sales_commission_management.payout.employee_payment import (
            get_payroll_evidence_level,
        )
        # Bank entry without payroll entry -> None
        self.assertIsNone(get_payroll_evidence_level(bank_entry="NONEXISTENT-JE"))

    def test_get_payroll_evidence_level_payroll_entry_requires_bank_entry(self):
        from sales_commission_management.sales_commission_management.payout.employee_payment import (
            get_payroll_evidence_level,
        )
        # Payroll entry without bank entry -> None (not Payroll Bank Entry)
        self.assertIsNone(get_payroll_evidence_level(payroll_entry="NONEXISTENT-PE"))

    def test_validate_employee_payroll_link_missing_as(self):
        from sales_commission_management.sales_commission_management.payout.employee_payment import (
            validate_employee_payroll_link,
        )
        self.assertRaises(frappe.ValidationError, validate_employee_payroll_link, "NONEXISTENT-SETT", "NONEXISTENT-AS")

    def test_validate_employee_payroll_link_bank_entry_requires_payroll_entry(self):
        from sales_commission_management.sales_commission_management.payout.employee_payment import (
            validate_employee_payroll_link,
        )
        self.assertRaises(frappe.ValidationError, validate_employee_payroll_link, "NONEXISTENT-SETT", "NONEXISTENT-AS", None, None, "NONEXISTENT-JE")

    def test_get_employee_settlement_for_additional_salary_missing(self):
        from sales_commission_management.sales_commission_management.payout.employee_payment import (
            get_employee_settlement_for_additional_salary,
        )
        result = get_employee_settlement_for_additional_salary("NONEXISTENT-AS")
        self.assertIsNone(result)