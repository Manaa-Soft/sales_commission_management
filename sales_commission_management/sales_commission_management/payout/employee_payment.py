import frappe
from frappe import _
from frappe.utils import flt, cint
from sales_commission_management.sales_commission_management.utils import currency_precision


def get_employee_settlement_for_additional_salary(additional_salary_name):
    """Fail-closed matching of Additional Salary to Commission Settlement.

    Returns the settlement name if the Additional Salary is linked to a
    submitted Commission Settlement. Returns None for a successful non-match.
    Raises on database errors.
    """
    if not additional_salary_name:
        return None

    as_doc = frappe.get_doc("Additional Salary", additional_salary_name)
    if as_doc.docstatus != 1:
        return None

    ref_doctype = getattr(as_doc, "ref_doctype", None)
    ref_docname = getattr(as_doc, "ref_docname", None)

    if ref_doctype != "Commission Settlement":
        return None

    if not ref_docname:
        return None

    # Verify the settlement exists and is submitted.
    sett = frappe.get_value(
        "Commission Settlement",
        ref_docname,
        ["name", "docstatus", "company", "beneficiary_type", "employee"],
        as_dict=True,
    )
    if not sett or sett.docstatus != 1:
        return None

    # Verify employee matches.
    if sett.employee and sett.employee != as_doc.employee:
        return None

    # Verify company matches.
    emp_company = frappe.db.get_value("Employee", as_doc.employee, "company")
    if emp_company and sett.company and emp_company != sett.company:
        return None

    return sett.name


def resolve_employee_payroll_currencies(settlement, additional_salary):
    """Resolve currencies for an employee payroll payout.

    Returns a dict with:
        salary_currency: currency of the Additional Salary / payroll
        commission_currency: commission currency from settlement
        company_currency: company accounting currency
        settlement_amount: amount in settlement currency
        commission_amount: amount in commission currency
        company_amount: amount in company currency
        fx_rate: conversion rate (commission -> company)
        fx_date: effective date for the rate
    """
    sett = frappe.get_doc("Commission Settlement", settlement)

    # Salary currency from Additional Salary.
    salary_currency = getattr(additional_salary, "currency", None)
    if not salary_currency:
        # Fall back to employee's salary structure assignment.
        fx_date = sett.period_from or frappe.utils.nowdate()
        if frappe.db.exists("DocType", "Salary Structure Assignment"):
            ssa = frappe.get_all(
                "Salary Structure Assignment",
                filters={
                    "employee": additional_salary.employee,
                    "docstatus": 1,
                    "from_date": ["<=", fx_date],
                },
                fields=["currency"],
                order_by="from_date desc",
                limit=1,
            )
            if ssa:
                salary_currency = ssa[0].get("currency")
    if not salary_currency:
        salary_currency = frappe.db.get_value("Company", sett.company, "default_currency")

    commission_currency = sett.commission_currency
    company_currency = sett.company and frappe.db.get_value("Company", sett.company, "default_currency")

    # Amounts.
    settlement_amount = flt(sett.total_payable)
    commission_amount = flt(sett.total_payable)  # commission currency = settlement currency for now
    company_amount = flt(sett.total_payable)

    # FX rate (commission -> company).
    fx_rate = 1.0
    fx_date = sett.period_from or frappe.utils.nowdate()
    if commission_currency and company_currency and commission_currency != company_currency:
        rate = flt(sett.settlement_fx_rate)
        if not rate or rate <= 0:
            from frappe.utils import get_exchange_rate
            rate = get_exchange_rate(commission_currency, company_currency, fx_date)
        if not rate or rate <= 0:
            frappe.throw(
                _("Missing exchange rate from {0} to {1}.").format(commission_currency, company_currency)
            )
        fx_rate = rate
        company_amount = flt(settlement_amount * rate, currency_precision(company_currency))

    return {
        "salary_currency": salary_currency,
        "commission_currency": commission_currency,
        "company_currency": company_currency,
        "settlement_amount": settlement_amount,
        "commission_amount": commission_amount,
        "company_amount": company_amount,
        "fx_rate": fx_rate,
        "fx_date": fx_date,
    }


def get_payroll_evidence_level(salary_slip=None, payroll_entry=None, bank_entry=None):
    """Determine the evidence level for an employee payroll payout.

    Returns one of:
        'Payroll Accrued' - Salary Slip submitted, no bank entry
        'Disbursement Recorded' - Payroll Bank Entry submitted
        'Bank Reconciled' - External bank evidence confirmed
    """
    if bank_entry:
        return "Bank Reconciled"
    if payroll_entry:
        return "Payroll Bank Entry"
    if salary_slip:
        return "Payroll Accrued"
    return "Payroll Accrued"


def validate_employee_payroll_link(settlement, additional_salary, salary_slip=None, payroll_entry=None):
    """Validate the employee payroll link chain.

    Ensures:
    - Additional Salary is linked to the settlement
    - Salary Slip (if provided) includes the Additional Salary
    - Payroll Entry (if provided) matches the Salary Slip
    - Employee matches across all documents
    - Company matches across all documents

    Raises frappe.ValidationError on any mismatch.
    """
    sett = frappe.get_doc("Commission Settlement", settlement)
    as_doc = frappe.get_doc("Additional Salary", additional_salary)

    if as_doc.ref_doctype != "Commission Settlement" or as_doc.ref_docname != settlement:
        frappe.throw(_("Additional Salary must be linked to this settlement."))

    if as_doc.employee != sett.employee:
        frappe.throw(_("Additional Salary employee must match settlement employee."))

    emp_company = frappe.db.get_value("Employee", as_doc.employee, "company")
    if emp_company and emp_company != sett.company:
        frappe.throw(_("Employee company must match settlement company."))

    if salary_slip:
        ss = frappe.get_doc("Salary Slip", salary_slip)
        if ss.employee != as_doc.employee:
            frappe.throw(_("Salary Slip employee must match Additional Salary employee."))
        if ss.company != sett.company:
            frappe.throw(_("Salary Slip company must match settlement company."))
        # Verify the Salary Slip includes this Additional Salary.
        found = False
        for row in (getattr(ss, "earnings", []) or []):
            if getattr(row, "salary_component", None) == as_doc.salary_component:
                found = True
                break
        if not found:
            frappe.throw(_("Salary Slip does not include the commission Additional Salary component."))

    if payroll_entry:
        pe = frappe.get_doc("Payroll Entry", payroll_entry)
        if pe.company != sett.company:
            frappe.throw(_("Payroll Entry company must match settlement company."))