from frappe.tests import IntegrationTestCase
import frappe
from types import SimpleNamespace
from unittest.mock import patch


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

    def test_bank_transaction_without_salary_slip_rejected(self):
        from sales_commission_management.sales_commission_management.payout.employee_payment import (
            validate_employee_payroll_link,
        )
        settlement = SimpleNamespace(
            docstatus=1,
            beneficiary_type="Sales Person",
            company="COMPANY-A",
            employee="EMP-TEST",
            settlement_method="Additional Salary",
            settlement_document_type="Additional Salary",
            settlement_document="AS-TEST",
        )
        additional_salary = SimpleNamespace(
            docstatus=1,
            ref_doctype="Commission Settlement",
            ref_docname="SETT-TEST",
            employee="EMP-TEST",
            company="COMPANY-A",
        )
        with patch(
            "sales_commission_management.sales_commission_management.payout.employee_payment.frappe.get_doc",
            side_effect=lambda dt, name: settlement if dt == "Commission Settlement" else additional_salary,
        ):
            with self.assertRaises(frappe.ValidationError):
                validate_employee_payroll_link(
                    settlement="SETT-TEST",
                    additional_salary="AS-TEST",
                    salary_slip=None,
                    payroll_entry="PE-TEST",
                    bank_entry="JE-TEST",
                    bank_transaction="BT-TEST",
                )

    def test_journal_entry_company_mismatch_rejected(self):
        from sales_commission_management.sales_commission_management.payout.employee_payment import (
            validate_employee_payroll_link,
        )
        settlement = SimpleNamespace(
            docstatus=1,
            beneficiary_type="Sales Person",
            company="COMPANY-A",
            employee="EMP-TEST",
            settlement_method="Additional Salary",
            settlement_document_type="Additional Salary",
            settlement_document="AS-TEST",
        )
        additional_salary = SimpleNamespace(
            docstatus=1,
            ref_doctype="Commission Settlement",
            ref_docname="SETT-TEST",
            employee="EMP-TEST",
            company="COMPANY-A",
        )
        salary_slip = SimpleNamespace(
            docstatus=1,
            employee="EMP-TEST",
            company="COMPANY-A",
            payroll_entry="PE-TEST",
            earnings=[SimpleNamespace(additional_salary="AS-TEST")],
        )
        payroll_entry = SimpleNamespace(
            docstatus=1,
            company="COMPANY-A",
        )

        def mock_get_doc(dt, name):
            if dt == "Commission Settlement":
                return settlement
            if dt == "Additional Salary":
                return additional_salary
            if dt == "Salary Slip":
                return salary_slip
            if dt == "Payroll Entry":
                return payroll_entry
            if dt == "Journal Entry":
                je = SimpleNamespace(docstatus=1, company="COMPANY-B", voucher_type="Bank Entry")
                return je
            return None

        def mock_db_exists(dt, name):
            return True

        def mock_db_get_value(dt, name, field, as_dict=False):
            if dt == "Employee" and field == "company":
                return "COMPANY-A"
            if dt == "Journal Entry" and field == "docstatus":
                return 1
            if dt == "Journal Entry" and field == "voucher_type":
                return "Bank Entry"
            if dt == "Journal Entry" and field == "company":
                return "COMPANY-B"
            return None

        with patch(
            "sales_commission_management.sales_commission_management.payout.employee_payment.frappe.get_doc",
            side_effect=mock_get_doc,
        ):
            with patch(
                "sales_commission_management.sales_commission_management.payout.employee_payment.frappe.db.exists",
                side_effect=mock_db_exists,
            ):
                with patch(
                    "sales_commission_management.sales_commission_management.payout.employee_payment.frappe.db.get_value",
                    side_effect=mock_db_get_value,
                ):
                    with self.assertRaises(frappe.ValidationError):
                        validate_employee_payroll_link(
                            settlement="SETT-TEST",
                            additional_salary="AS-TEST",
                            salary_slip="SS-TEST",
                            payroll_entry="PE-TEST",
                            bank_entry="JE-TEST",
                        )

    def test_get_employee_settlement_for_additional_salary_missing(self):
        from sales_commission_management.sales_commission_management.payout.employee_payment import (
            get_employee_settlement_for_additional_salary,
        )
        result = get_employee_settlement_for_additional_salary("NONEXISTENT-AS")
        self.assertIsNone(result)