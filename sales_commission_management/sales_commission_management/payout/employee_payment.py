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

    sett = frappe.get_value(
        "Commission Settlement",
        ref_docname,
        ["name", "docstatus", "company", "beneficiary_type", "employee",
         "settlement_method", "settlement_document_type", "settlement_document"],
        as_dict=True,
    )
    if not sett or sett.docstatus != 1:
        return None

    # All three link fields must be exact.
    if sett.settlement_method != "Additional Salary":
        return None
    if sett.settlement_document_type != "Additional Salary":
        return None
    if sett.settlement_document != additional_salary_name:
        return None

    if not sett.employee:
        return None
    if sett.employee != as_doc.employee:
        return None

    if not sett.company or not as_doc.company or sett.company != as_doc.company:
        return None
    emp_company = frappe.db.get_value("Employee", as_doc.employee, "company")
    if emp_company and emp_company != sett.company:
        return None

    return sett.name


def resolve_employee_payroll_currencies(settlement, additional_salary):
    """Resolve currencies and amounts for an employee payroll payout.

    Keeps salary, commission, and company currencies/amounts distinct.
    Uses saved FX snapshots where available; only looks up new rates
    when no saved snapshot exists.

    Returns a dict with:
        salary_currency, salary_amount
        commission_currency, commission_amount
        company_currency, company_amount
        salary_to_commission_rate, salary_to_commission_date
        commission_to_company_rate, commission_to_company_date
    """
    sett = frappe.get_doc("Commission Settlement", settlement)
    as_doc = frappe.get_doc("Additional Salary", additional_salary)

    if sett.docstatus != 1:
        frappe.throw(_("Commission Settlement must be submitted."))

    # Require Additional Salary to be submitted.
    if as_doc.docstatus != 1:
        frappe.throw(_("Additional Salary must be submitted."))

    # Validate Additional Salary is linked to this settlement.
    if as_doc.ref_doctype != "Commission Settlement" or as_doc.ref_docname != settlement:
        frappe.throw(_("Additional Salary must be linked to this settlement."))
    if sett.settlement_method != "Additional Salary":
        frappe.throw(_("Settlement method must be Additional Salary."))
    if sett.settlement_document_type != "Additional Salary":
        frappe.throw(_("Settlement document type must be Additional Salary."))
    if sett.settlement_document != additional_salary:
        frappe.throw(_("Settlement document must match the Additional Salary."))
    if as_doc.employee != sett.employee:
        frappe.throw(_("Additional Salary employee must match settlement employee."))
    if not as_doc.company or as_doc.company != sett.company:
        frappe.throw(_("Additional Salary company must match settlement company."))

    # Validate Additional Salary is linked to this settlement.
    if as_doc.ref_doctype != "Commission Settlement" or as_doc.ref_docname != settlement:
        frappe.throw(_("Additional Salary must be linked to this settlement."))
    if sett.settlement_method != "Additional Salary":
        frappe.throw(_("Settlement method must be Additional Salary."))
    if sett.settlement_document_type != "Additional Salary":
        frappe.throw(_("Settlement document type must be Additional Salary."))
    if sett.settlement_document != additional_salary:
        frappe.throw(_("Settlement document must match the Additional Salary."))
    if as_doc.employee != sett.employee:
        frappe.throw(_("Additional Salary employee must match settlement employee."))
    if not as_doc.company or as_doc.company != sett.company:
        frappe.throw(_("Additional Salary company must match settlement company."))

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
                fields=["currency", "from_date"],
                order_by="from_date desc",
                limit=1,
            )
            if ssa:
                salary_currency = ssa[0].get("currency")
    if not salary_currency:
        frappe.throw(
            _("Cannot resolve salary currency for employee {0}.").format(as_doc.employee)
        )

    commission_currency = sett.commission_currency
    if not commission_currency:
        frappe.throw(_("Commission currency is required on the settlement."))

    company_currency = frappe.db.get_value("Company", sett.company, "default_currency")
    if not company_currency:
        frappe.throw(_("Cannot resolve company currency."))

    emp_company = frappe.db.get_value("Employee", as_doc.employee, "company")
    if emp_company and emp_company != sett.company:
        frappe.throw(
            _("Employee company ({0}) must match settlement company ({1}).").format(emp_company, sett.company)
        )

    salary_amount = flt(getattr(as_doc, "amount", 0))
    if salary_amount <= 0 or not _is_finite_positive(salary_amount):
        frappe.throw(_("Additional Salary amount must be a finite positive number."))

    commission_amount = flt(sett.total_payable)
    if commission_amount <= 0 or not _is_finite_positive(commission_amount):
        frappe.throw(_("Settlement total payable must be a finite positive number."))

    # Company amount: convert commission to company currency.
    company_amount = commission_amount
    commission_to_company_rate = 1.0
    commission_to_company_date = payroll_date
    if commission_currency != company_currency:
        # Reuse saved settlement FX rate only when salary_currency == company_currency
        # (meaning the saved rate is commission->company, not commission->salary).
        if salary_currency == company_currency and flt(sett.settlement_fx_rate) > 0:
            commission_to_company_rate = flt(sett.settlement_fx_rate)
        else:
            from frappe.utils import get_exchange_rate
            rate = get_exchange_rate(commission_currency, company_currency, payroll_date)
            if not rate or not _is_finite_positive(rate):
                frappe.throw(
                    _("Missing exchange rate from {0} to {1}.").format(commission_currency, company_currency)
                )
            commission_to_company_rate = rate
        company_amount = flt(commission_amount * commission_to_company_rate, currency_precision(company_currency))

    # Salary to commission rate.
    salary_to_commission_rate = 1.0
    salary_to_commission_date = payroll_date
    if salary_currency != commission_currency:
        # Reuse inverse of saved settlement FX rate when currencies are
        # swapped (saved rate is commission->salary, need salary->commission).
        saved_rate = flt(sett.settlement_fx_rate)
        saved_date = getattr(sett, "settlement_fx_date", None)
        if saved_rate > 0 and _is_finite_positive(saved_rate):
            # Saved rate is commission->salary; inverse is salary->commission.
            inverse = 1.0 / saved_rate
            if _is_finite_positive(inverse):
                salary_to_commission_rate = inverse
                if saved_date:
                    salary_to_commission_date = saved_date
            else:
                from frappe.utils import get_exchange_rate
                rate = get_exchange_rate(salary_currency, commission_currency, payroll_date)
                if not rate or not _is_finite_positive(rate):
                    frappe.throw(
                        _("Missing exchange rate from {0} to {1}.").format(salary_currency, commission_currency)
                    )
                salary_to_commission_rate = rate
        else:
            from frappe.utils import get_exchange_rate
            rate = get_exchange_rate(salary_currency, commission_currency, payroll_date)
            if not rate or not _is_finite_positive(rate):
                frappe.throw(
                    _("Missing exchange rate from {0} to {1}.").format(salary_currency, commission_currency)
                )
            salary_to_commission_rate = rate

    # Validate finite amounts.
    if not _is_finite_positive(company_amount):
        frappe.throw(_("Company amount must be a finite positive number."))

    return {
        "salary_currency": salary_currency,
        "salary_amount": salary_amount,
        "commission_currency": commission_currency,
        "commission_amount": commission_amount,
        "company_currency": company_currency,
        "company_amount": company_amount,
        "salary_to_commission_rate": salary_to_commission_rate,
        "salary_to_commission_date": salary_to_commission_date,
        "commission_to_company_rate": commission_to_company_rate,
        "commission_to_company_date": payroll_date,
    }


def _is_finite_positive(value):
    """Check if a value is finite and positive."""
    import math
    try:
        v = float(value)
        return math.isfinite(v) and v > 0
    except (TypeError, ValueError):
        return False


def get_payroll_evidence_level(salary_slip=None, payroll_entry=None, bank_entry=None, bank_transaction=None):
    """Determine the evidence level for an employee payroll payout.

    Evidence levels are based on verified document status and actual
    bank reconciliation, not just the presence of a reference.

    Returns one of:
        None - no evidence
        'Payroll Accrued' - Submitted Salary Slip (payroll calculated)
        'Payroll Bank Entry' - Submitted Payroll Entry with a linked, submitted Bank/Cash Journal Entry
        'Bank Reconciled' - Reconciled Bank Transaction linked to the payroll bank entry
    """
    if bank_transaction and bank_entry and payroll_entry:
        # If salary_slip is provided, verify it belongs to this payroll entry.
        if salary_slip and frappe.db.exists("Salary Slip", salary_slip):
            ss_pe = frappe.db.get_value("Salary Slip", salary_slip, "payroll_entry")
            if ss_pe and ss_pe != payroll_entry:
                return None
        # Verify bank transaction is reconciled and linked to the bank entry
        # which is linked to the payroll entry.
        if frappe.db.exists("Bank Transaction", bank_transaction):
            status = frappe.db.get_value("Bank Transaction", bank_transaction, "status")
            if status == "Reconciled":
                allocations = frappe.get_all(
                    "Bank Transaction Payments",
                    filters={
                        "parent": bank_transaction,
                        "payment_document": "Journal Entry",
                        "payment_entry": bank_entry,
                    },
                    limit=1,
                )
                if allocations:
                    # Verify bank entry is submitted and linked to payroll entry.
                    if frappe.db.exists("Journal Entry", bank_entry):
                        je_status = frappe.db.get_value("Journal Entry", bank_entry, "docstatus")
                        if je_status == 1:
                            # Verify voucher type is Bank Entry or Cash Entry.
                            voucher_type = frappe.db.get_value("Journal Entry", bank_entry, "voucher_type")
                            if voucher_type in ("Bank Entry", "Cash Entry"):
                                # Verify Journal Entry Account references the payroll entry.
                                linked = frappe.db.exists(
                                    "Journal Entry Account",
                                    {
                                        "parent": bank_entry,
                                        "reference_type": "Payroll Entry",
                                        "reference_name": payroll_entry,
                                    },
                                )
                                if linked:
                                    # Verify payroll entry is submitted.
                                    if frappe.db.exists("Payroll Entry", payroll_entry):
                                        pe_status = frappe.db.get_value("Payroll Entry", payroll_entry, "docstatus")
                                        if pe_status == 1:
                                            return "Bank Reconciled"
        return None
    if payroll_entry and bank_entry:
        # If salary_slip is provided, verify it belongs to this payroll entry.
        if salary_slip and frappe.db.exists("Salary Slip", salary_slip):
            ss_pe = frappe.db.get_value("Salary Slip", salary_slip, "payroll_entry")
            if ss_pe and ss_pe != payroll_entry:
                return None
        # Verify payroll entry is submitted.
        if frappe.db.exists("Payroll Entry", payroll_entry):
            pe_status = frappe.db.get_value("Payroll Entry", payroll_entry, "docstatus")
            if pe_status == 1:
                # Verify bank entry is submitted and is a Bank/Cash Entry.
                if frappe.db.exists("Journal Entry", bank_entry):
                    je_status = frappe.db.get_value("Journal Entry", bank_entry, "docstatus")
                    if je_status == 1:
                        voucher_type = frappe.db.get_value("Journal Entry", bank_entry, "voucher_type")
                        if voucher_type in ("Bank Entry", "Cash Entry"):
                            # Verify Journal Entry Account references the payroll entry.
                            linked = frappe.db.exists(
                                "Journal Entry Account",
                                {
                                    "parent": bank_entry,
                                    "reference_type": "Payroll Entry",
                                    "reference_name": payroll_entry,
                                },
                            )
                            if linked:
                                return "Payroll Bank Entry"
        return None
    if salary_slip:
        if not frappe.db.exists("Salary Slip", salary_slip):
            return None
        status = frappe.db.get_value("Salary Slip", salary_slip, "docstatus")
        if status != 1:
            return None
        return "Payroll Accrued"
    return None


def validate_employee_payroll_link(settlement, additional_salary, salary_slip=None, payroll_entry=None, bank_entry=None, bank_transaction=None):
    """Validate the employee payroll link chain.

    Ensures:
    - Additional Salary is linked to the settlement and is submitted.
    - Settlement side fields match exactly (bidirectional consistency).
    - Employee and company match across all documents.
    - Salary Slip (if provided) is submitted and includes the specific
      Additional Salary (verified by additional_salary Link field).
    - Payroll Entry (if provided) is submitted, matches company, and is
      linked to the Salary Slip.
    - Bank Entry (if provided) is submitted, is a Bank/Cash Entry, and
      references the Payroll Entry.

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

    # Exact reverse-link consistency (bidirectional).
    if as_doc.ref_doctype != "Commission Settlement" or as_doc.ref_docname != settlement:
        frappe.throw(_("Additional Salary must be linked to this settlement."))
    if sett.settlement_method != "Additional Salary":
        frappe.throw(_("Settlement method must be Additional Salary."))
    if sett.settlement_document_type != "Additional Salary":
        frappe.throw(_("Settlement document type must be Additional Salary."))
    if sett.settlement_document != additional_salary:
        frappe.throw(_("Settlement document must match the Additional Salary."))

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
        # Verify the Salary Slip includes this specific Additional Salary
        # using the additional_salary Link field on Salary Detail.
        found = False
        for row in (getattr(ss, "earnings", []) or []):
            if getattr(row, "additional_salary", None) == additional_salary:
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
            # Require Salary Slip to be linked to this Payroll Entry.
            ss_payroll_entry = getattr(ss, "payroll_entry", None)
            if not ss_payroll_entry:
                frappe.throw(_("Salary Slip must be linked to a Payroll Entry."))
            if ss_payroll_entry != payroll_entry:
                frappe.throw(_("Salary Slip is not linked to the specified Payroll Entry."))

        if bank_entry:
            if not frappe.db.exists("Journal Entry", bank_entry):
                frappe.throw(_("Bank Entry {0} does not exist.").format(bank_entry))
            je_status = frappe.db.get_value("Journal Entry", bank_entry, "docstatus")
            if je_status != 1:
                frappe.throw(_("Bank Entry must be submitted."))
            voucher_type = frappe.db.get_value("Journal Entry", bank_entry, "voucher_type")
            if voucher_type not in ("Bank Entry", "Cash Entry"):
                frappe.throw(_("Bank Entry must be a Bank or Cash Entry."))
            if not payroll_entry:
                frappe.throw(_("Bank Entry validation requires a Payroll Entry."))
            linked = frappe.db.exists(
                "Journal Entry Account",
                {
                    "parent": bank_entry,
                    "reference_type": "Payroll Entry",
                    "reference_name": payroll_entry,
                },
            )
            if not linked:
                frappe.throw(_("Bank Entry does not reference the Payroll Entry."))
            # bank_transaction validation is done independently below.

    # Validate bank_transaction independently (requires bank_entry and payroll_entry).
    if bank_transaction:
        if not bank_entry or not payroll_entry:
            frappe.throw(_("Bank Transaction validation requires Bank Entry and Payroll Entry."))
        if not frappe.db.exists("Bank Transaction", bank_transaction):
            frappe.throw(_("Bank Transaction {0} does not exist.").format(bank_transaction))
        bt_status = frappe.db.get_value("Bank Transaction", bank_transaction, "status")
        if bt_status != "Reconciled":
            frappe.throw(_("Bank Transaction must be Reconciled."))
        bt_docstatus = frappe.db.get_value("Bank Transaction", bank_transaction, "docstatus")
        if bt_docstatus != 1:
            frappe.throw(_("Bank Transaction must be submitted."))
        bt_company = frappe.db.get_value("Bank Transaction", bank_transaction, "company")
        if bt_company and sett.company and bt_company != sett.company:
            frappe.throw(_("Bank Transaction company must match settlement company."))
        allocations = frappe.get_all(
            "Bank Transaction Payments",
            filters={
                "parent": bank_transaction,
                "payment_document": "Journal Entry",
                "payment_entry": bank_entry,
            },
            limit=1,
        )
        if not allocations:
            frappe.throw(_("Bank Transaction does not reference the Bank Entry."))
    elif payroll_entry or bank_entry or bank_transaction:
        frappe.throw(_("Payroll Entry, Bank Entry, or Bank Transaction requires a Salary Slip for validation."))