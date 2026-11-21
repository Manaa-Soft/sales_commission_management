import frappe
from frappe import _
from frappe.utils import flt, cint, getdate
from sales_commission_management.sales_commission_management.utils import currency_precision


def get_employee_settlement_for_additional_salary(additional_salary_name):
    """Fail-closed matching of Additional Salary to Commission Settlement.

    Returns the settlement name if the Additional Salary is linked to a
    submitted Commission Settlement. Returns None for a successful non-match.
    Raises on database errors or invalid references.
    """
    if not additional_salary_name:
        return None

    # Fail closed: if the record doesn't exist, return None (not an error).
    if not frappe.db.exists("Additional Salary", additional_salary_name):
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
        ["name", "docstatus", "company", "beneficiary_type", "employee", "settlement_method", "settlement_document_type", "settlement_document"],
        as_dict=True,
    )
    if not sett or sett.docstatus != 1:
        return None

    # For employee payroll settlements, settlement_method must be Additional Salary.
    if sett.settlement_method and sett.settlement_method != "Additional Salary":
        return None

    # Settlement document must be linked correctly.
    if sett.settlement_document_type and sett.settlement_document_type != "Additional Salary":
        return None
    if sett.settlement_document and sett.settlement_document != additional_salary_name:
        return None

    # Require settlement employee to exist and match.
    if not sett.employee:
        return None
    if sett.employee != as_doc.employee:
        return None

    # Verify company consistency.
    if sett.company and as_doc.company and sett.company != as_doc.company:
        return None
    emp_company = frappe.db.get_value("Employee", as_doc.employee, "company")
    if emp_company and sett.company and emp_company != sett.company:
        return None

    return sett.name


def resolve_employee_payroll_currencies(settlement, additional_salary):
    """Resolve currencies and amounts for an employee payroll payout.

    Keeps salary, commission, and company currencies/amounts distinct.
    Does not reuse settlement_fx_rate (which is commission->salary) for
    company-currency conversion.

    Returns a dict with:
        salary_currency, salary_amount
        commission_currency, commission_amount
        company_currency, company_amount
        salary_to_commission_rate, salary_to_commission_date
        commission_to_company_rate, commission_to_company_date
    """
    sett = frappe.get_doc("Commission Settlement", settlement)
    as_doc = frappe.get_doc("Additional Salary", additional_salary)

    # Salary currency: from Additional Salary, or from Salary Structure Assignment.
    salary_currency = getattr(as_doc, "currency", None)
    payroll_date = getattr(as_doc, "payroll_date", None) or sett.period_from or frappe.utils.nowdate()

    if not salary_currency:
        if frappe.db.exists("DocType", "Salary Structure Assignment"):
            ssa = frappe.get_all(
                "Salary Structure Assignment",
                filters={
                    "employee": as_doc.employee,
                    "docstatus": 1,
                    "from_date": ["<=", payroll_date],
                },
                fields=["currency", "from_date", "to_date"],
                order_by="from_date desc",
                limit=1,
            )
            if ssa:
                # Verify the assignment covers the payroll date.
                assgn = ssa[0]
                to_date = getattr(assgn, "to_date", None)
                if to_date and getdate(payroll_date) > getdate(to_date):
                    # Assignment expired; reject.
                    frappe.throw(
                        _("Salary Structure Assignment for employee {0} expired on {1}.").format(
                            as_doc.employee, to_date
                        )
                    )
                salary_currency = assgn.get("currency")
    if not salary_currency:
        frappe.throw(
            _("Cannot resolve salary currency for employee {0}.").format(as_doc.employee)
        )

    commission_currency = sett.commission_currency
    company_currency = frappe.db.get_value("Company", sett.company, "default_currency")

    # Salary amount: actual Additional Salary amount.
    salary_amount = flt(getattr(as_doc, "amount", 0))

    # Commission amount: settlement total_payable (commission currency).
    commission_amount = flt(sett.total_payable)

    # Company amount: convert commission to company currency.
    company_amount = commission_amount
    commission_to_company_rate = 1.0
    commission_to_company_date = payroll_date
    if commission_currency and company_currency and commission_currency != company_currency:
        # Use settlement FX rate only if commission_currency == salary_currency.
        # Otherwise, we need a separate commission->company rate.
        if commission_currency == salary_currency and flt(sett.settlement_fx_rate) > 0:
            commission_to_company_rate = flt(sett.settlement_fx_rate)
        else:
            from frappe.utils import get_exchange_rate
            rate = get_exchange_rate(commission_currency, company_currency, payroll_date)
            if not rate or rate <= 0:
                frappe.throw(
                    _("Missing exchange rate from {0} to {1}.").format(commission_currency, company_currency)
                )
            commission_to_company_rate = rate
        company_amount = flt(commission_amount * commission_to_company_rate, currency_precision(company_currency))

    # Salary to commission rate (if different).
    salary_to_commission_rate = 1.0
    if salary_currency and commission_currency and salary_currency != commission_currency:
        # If salary == commission, rate is 1.0. Otherwise we need a rate.
        if salary_currency == commission_currency:
            salary_to_commission_rate = 1.0
        else:
            from frappe.utils import get_exchange_rate
            rate = get_exchange_rate(salary_currency, commission_currency, payroll_date)
            if not rate or rate <= 0:
                frappe.throw(
                    _("Missing exchange rate from {0} to {1}.").format(salary_currency, commission_currency)
                )
            salary_to_commission_rate = rate

    return {
        "salary_currency": salary_currency,
        "salary_amount": salary_amount,
        "commission_currency": commission_currency,
        "commission_amount": commission_amount,
        "company_currency": company_currency,
        "company_amount": company_amount,
        "salary_to_commission_rate": salary_to_commission_rate,
        "salary_to_commission_date": payroll_date,
        "commission_to_company_rate": commission_to_company_rate,
        "commission_to_company_date": payroll_date,
    }


def get_payroll_evidence_level(salary_slip=None, payroll_entry=None, bank_entry=None):
    """Determine the evidence level for an employee payroll payout.

    Evidence levels are based on verified document status, not just
    the presence of a reference.

    Returns one of:
        None - no evidence
        'Payroll Accrued' - Submitted Salary Slip (payroll calculated)
        'Payroll Bank Entry' - Submitted Payroll Entry (disbursement recorded)
        'Bank Reconciled' - Verified bank reconciliation evidence
    """
    if bank_entry:
        # Verify bank entry is submitted.
        if frappe.db.exists("Journal Entry", bank_entry):
            status = frappe.db.get_value("Journal Entry", bank_entry, "docstatus")
            if status == 1:
                return "Bank Reconciled"
        return None
    if payroll_entry:
        if frappe.db.exists("Payroll Entry", payroll_entry):
            status = frappe.db.get_value("Payroll Entry", payroll_entry, "docstatus")
            if status == 1:
                return "Payroll Bank Entry"
        return None
    if salary_slip:
        if frappe.db.exists("Salary Slip", salary_slip):
            status = frappe.db.get_value("Salary Slip", salary_slip, "docstatus")
            if status == 1:
                return "Payroll Accrued"
        return None
    return None


def validate_employee_payroll_link(settlement, additional_salary, salary_slip=None, payroll_entry=None):
    """Validate the employee payroll link chain.

    Ensures:
    - Additional Salary is linked to the settlement and is submitted.
    - Employee and company match across all documents.
    - Salary Slip (if provided) is submitted and includes the Additional Salary
      component (verified by component, not just any matching component).
    - Payroll Entry (if provided) is submitted and is linked to the Salary Slip.

    Raises frappe.ValidationError on any mismatch.
    """
    sett = frappe.get_doc("Commission Settlement", settlement)
    if sett.docstatus != 1:
        frappe.throw(_("Commission Settlement must be submitted."))
    if sett.beneficiary_type != "Sales Person":
        frappe.throw(_("Settlement must be for a Sales Person."))

    if not frappe.db.exists("Additional Salary", additional_salary):
        frappe.throw(_("Additional Salary {0} does not exist.").format(additional_salary))
    as_doc = frappe.get_doc("Additional Salary", additional_salary)
    if as_doc.docstatus != 1:
        frappe.throw(_("Additional Salary must be submitted."))

    if as_doc.ref_doctype != "Commission Settlement" or as_doc.ref_docname != settlement:
        frappe.throw(_("Additional Salary must be linked to this settlement."))

    if as_doc.employee != sett.employee:
        frappe.throw(_("Additional Salary employee must match settlement employee."))

    emp_company = frappe.db.get_value("Employee", as_doc.employee, "company")
    if emp_company and emp_company != sett.company:
        frappe.throw(_("Employee company must match settlement company."))

    if salary_slip:
        if not frappe.db.exists("Salary Slip", salary_slip):
            frappe.throw(_("Salary Slip {0} does not exist.").format(salary_slip))
        ss = frappe.get_doc("Salary Slip", salary_slip)
        if ss.docstatus != 1:
            frappe.throw(_("Salary Slip must be submitted."))
        if ss.employee != as_doc.employee:
            frappe.throw(_("Salary Slip employee must match Additional Salary employee."))
        if ss.company != sett.company:
            frappe.throw(_("Salary Slip company must match settlement company."))
        # Verify the Salary Slip includes this specific Additional Salary.
        # Check by salary_component AND amount match.
        found = False
        for row in (getattr(ss, "earnings", []) or []):
            if (getattr(row, "salary_component", None) == as_doc.salary_component
                    and flt(getattr(row, "amount", 0)) == flt(as_doc.amount)):
                found = True
                break
        if not found:
            frappe.throw(_("Salary Slip does not include the commission Additional Salary."))

        if payroll_entry:
            if not frappe.db.exists("Payroll Entry", payroll_entry):
                frappe.throw(_("Payroll Entry {0} does not exist.").format(payroll_entry))
            pe = frappe.get_doc("Payroll Entry", payroll_entry)
            if pe.docstatus != 1:
                frappe.throw(_("Payroll Entry must be submitted."))
            if pe.company != sett.company:
                frappe.throw(_("Payroll Entry company must match settlement company."))
            # Verify the Salary Slip is linked to this Payroll Entry.
            # Check if the Salary Slip's payroll_entry field matches.
            ss_payroll_entry = getattr(ss, "payroll_entry", None)
            if ss_payroll_entry and ss_payroll_entry != payroll_entry:
                frappe.throw(_("Salary Slip is not linked to the specified Payroll Entry."))
    elif payroll_entry:
        frappe.throw(_("Payroll Entry requires a Salary Slip for validation."))