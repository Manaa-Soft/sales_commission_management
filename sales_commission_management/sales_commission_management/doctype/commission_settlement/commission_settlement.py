import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt, now_datetime


class CommissionSettlement(Document):
    def validate(self):
        self._resolve_beneficiary()
        self._validate_entries()
        self._compute_totals()

    def _resolve_beneficiary(self):
        if self.beneficiary:
            return
        # Prefer an explicit sales_person / sales_partner selection.
        if self.sales_person:
            self.beneficiary = self.sales_person
            return
        if self.sales_partner:
            self.beneficiary = self.sales_partner
            return
        # Fall back to the first settlement item's beneficiary or its linked
        # Commission Entry.
        for row in self.entries or []:
            if getattr(row, "beneficiary", None):
                self.beneficiary = row.beneficiary
                return
            if getattr(row, "commission_ledger_entry", None):
                try:
                    entry = frappe.get_doc(
                        "Commission Entry", row.commission_ledger_entry
                    )
                    ben = entry.sales_person or entry.sales_partner
                    if ben:
                        self.beneficiary = ben
                        return
                except Exception:
                    continue
        if not self.beneficiary:
            self.beneficiary = "(group)"

    def _validate_entries(self):
        """Server-side allocation integrity checks.

        Ensures that no Commission Entry is double-allocated beyond its
        available balance and that the same entry is not listed twice.
        """
        seen = set()
        for row in self.entries or []:
            entry_name = getattr(row, "commission_ledger_entry", None)
            if not entry_name:
                continue
            if entry_name in seen:
                frappe.throw(
                    _(
                        "Commission Entry {0} is listed more than once in this settlement."
                    ).format(entry_name)
                )
            seen.add(entry_name)

            available = _available_balance_for_entry(
                entry_name, exclude_settlement=self.name
            )
            included = flt(row.amount_included)
            if included < 0:
                frappe.throw(
                    _("Amount included cannot be negative for {0}.").format(entry_name)
                )
            if included > available + 0.005:
                frappe.throw(
                    _(
                        "Amount included ({0}) exceeds the available balance ({1}) "
                        "for Commission Entry {2}."
                    ).format(included, available, entry_name)
                )
            # keep read-only balances in sync with the source entry
            try:
                entry = frappe.get_doc("Commission Entry", entry_name)
                row.earned_balance = flt(getattr(entry, "earned_commission", 0))
                row.eligible_balance = flt(getattr(entry, "payable_amount", 0))
                row.reserved_amount = flt(
                    _reserved_for_entry(entry_name, exclude_settlement=self.name)
                )
                row.already_settled_amount = flt(getattr(entry, "paid_amount", 0))
                if not row.beneficiary:
                    row.beneficiary = entry.sales_person or entry.sales_partner
                if not row.original_source_invoice:
                    row.original_source_invoice = getattr(entry, "sales_invoice", None)
            except Exception:
                continue

    def before_submit(self):
        self._compute_totals()
        if flt(self.total_payable) <= 0:
            frappe.throw(
                _("Total payable must be greater than zero to submit a settlement.")
            )

    def on_submit(self):
        self.status = "Submitted"
        self.approval_user = frappe.session.user
        self.approval_date = now_datetime()
        self._create_settlement_document()
        self._clear_accrued_liability()
        self.save(ignore_permissions=True)

    def _clear_accrued_liability(self):
        accrued_total = 0.0
        for row in self.entries or []:
            try:
                entry = frappe.get_doc("Commission Entry", row.commission_ledger_entry)
                accrued_total += flt(getattr(entry, "accrued_amount", 0))
            except Exception:
                continue
        if accrued_total <= 0.005:
            return
        try:
            settings = (
                frappe.get_single("Commission Settings")
                if frappe.db.exists("Commission Settings")
                else None
            )
        except Exception:
            settings = None
        accrued_account = None
        payable_account = None
        if settings:
            accrued_account = (
                getattr(settings, "default_accrued_liability_account", None) or None
            )
            payable_account = (
                getattr(settings, "default_commission_payable_account", None) or None
            )
        if not accrued_account or not payable_account:
            frappe.throw(
                _(
                    "Accrued liability and commission payable accounts must be configured in Commission Settings before settlement."
                )
            )
        je = frappe.get_doc(
            {
                "doctype": "Journal Entry",
                "voucher_type": "Journal Entry",
                "company": self.company,
                "posting_date": frappe.utils.nowdate(),
                "accounts": [
                    {
                        "account": accrued_account,
                        "debit_in_account_currency": flt(accrued_total),
                    },
                    {
                        "account": payable_account,
                        "credit_in_account_currency": flt(accrued_total),
                    },
                ],
                "user_remark": f"Clear accrued commission liability for settlement {self.name}",
            }
        )
        je.insert(ignore_permissions=True)
        je.submit()

    def on_cancel(self):
        self._cancel_settlement_document()
        self.status = "Cancelled"
        self.save(ignore_permissions=True)

    def _compute_totals(self):
        total_earned = 0.0
        total_payable = 0.0
        total_reserved = 0.0
        total_paid = 0.0
        for row in self.entries or []:
            total_earned += flt(row.earned_balance)
            # Total payable follows the amount actually included in this
            # settlement, not the whole eligible balance of each entry.
            total_payable += flt(row.amount_included) or flt(row.eligible_balance)
            total_reserved += flt(row.reserved_amount)
            total_paid += flt(row.already_settled_amount)
            if not row.amount_included:
                row.amount_included = flt(row.eligible_balance)
        self.total_earned = flt(total_earned, 2)
        self.total_payable = flt(total_payable, 2)
        self.total_reserved = flt(total_reserved, 2)
        self.total_paid = flt(total_paid, 2)
        self.outstanding = flt(total_payable - total_paid, 2)

    def _create_settlement_document(self):
        if self.settlement_method == "Additional Salary":
            self._create_additional_salary()
        elif self.settlement_method == "Supplier Purchase Invoice":
            self._create_purchase_invoice()
        else:
            self.settlement_document_type = None
            self.settlement_document = None
            self.status = "Approved"

    def _create_additional_salary(self):
        if not self.employee:
            frappe.throw(_("Employee is required for Additional Salary settlement."))
        if not self.company:
            frappe.throw(_("Company is required for settlement."))
        salary_component = (
            frappe.db.get_single_value(
                "Commission Settings", "default_salary_component"
            )
            if frappe.db.exists("Commission Settings")
            else None
        )
        if not salary_component:
            salary_component = (
                "Commission"
                if frappe.db.exists("Salary Component", "Commission")
                else None
            )

        doc = frappe.get_doc(
            {
                "doctype": "Additional Salary",
                "employee": self.employee,
                "salary_component": salary_component,
                "amount": flt(self.total_payable),
                "pay_from_date": self.period_from,
                "pay_to_date": self.period_to,
                "company": self.company,
                "ref_doctype": "Commission Settlement",
                "ref_docname": self.name,
            }
        )
        doc.insert(ignore_permissions=True)
        doc.submit()
        self.settlement_document_type = "Additional Salary"
        self.settlement_document = doc.name

    def _create_purchase_invoice(self):
        if not self.supplier:
            frappe.throw(
                _("Supplier is required for Supplier Purchase Invoice settlement.")
            )
        if not self.company:
            frappe.throw(_("Company is required for settlement."))

        item_code = (
            frappe.db.get_single_value("Commission Settings", "default_commission_item")
            if frappe.db.exists("Commission Settings")
            else None
        )
        if not item_code:
            item_code = frappe.db.get_value("Item", {"item_name": "Commission"}, "name")

        doc = frappe.get_doc(
            {
                "doctype": "Purchase Invoice",
                "supplier": self.supplier,
                "company": self.company,
                "posting_date": frappe.utils.nowdate(),
                "items": [
                    {
                        "item_code": item_code,
                        "qty": 1,
                        "rate": flt(self.total_payable),
                    }
                ],
                "remarks": f"Commission settlement {self.name}",
            }
        )
        doc.insert(ignore_permissions=True)
        doc.submit()
        self.settlement_document_type = "Purchase Invoice"
        self.settlement_document = doc.name

    def _cancel_settlement_document(self):
        if not self.settlement_document or not self.settlement_document_type:
            return
        try:
            doc = frappe.get_doc(
                self.settlement_document_type, self.settlement_document
            )
            if doc.docstatus == 1:
                doc.cancel()
        except Exception:
            frappe.log_error(
                frappe.get_traceback(), "Commission Settlement cancel failed"
            )


# ---------------------------------------------------------------------------
# Helpers / whitelisted fetch
# ---------------------------------------------------------------------------


def _reserved_for_entry(entry_name, exclude_settlement=None):
    """Sum of amounts reserved by other (submitted) settlements for an entry."""
    total = 0.0
    try:
        # Find submitted settlements (excluding the current one) that contain
        # this entry, then sum their reserved amounts.
        sett_filters = {"docstatus": 1}
        if exclude_settlement:
            sett_filters["name"] = ["!=", exclude_settlement]
        settlements = frappe.get_all(
            "Commission Settlement",
            filters=sett_filters,
            fields=["name"],
        )
    except Exception:
        settlements = []
    if not settlements:
        return total
    settlement_names = [s.name for s in settlements]
    try:
        items = frappe.get_all(
            "Commission Settlement Item",
            filters={
                "commission_ledger_entry": entry_name,
                "parent": ["in", settlement_names],
            },
            fields=["amount_included"],
        )
    except Exception:
        items = []
    for r in items or []:
        total += flt(r.get("amount_included"))
    return total


def _available_balance_for_entry(entry_name, exclude_settlement=None):
    try:
        entry = frappe.get_doc("Commission Entry", entry_name)
    except Exception:
        return 0.0
    earned = flt(getattr(entry, "earned_commission", 0))
    eligible = flt(getattr(entry, "payable_amount", 0))
    paid = flt(getattr(entry, "paid_amount", 0))
    reserved_other = _reserved_for_entry(entry_name, exclude_settlement=exclude_settlement)
    cap = eligible if eligible > 0 else earned
    available = cap - reserved_other - paid
    if available < 0:
        available = 0.0
    return flt(available, 2)


@frappe.whitelist()
def get_eligible_commission_entries(
    company=None,
    beneficiary_type=None,
    sales_person=None,
    sales_partner=None,
    period_from=None,
    period_to=None,
    commission_currency=None,
    exclude_settlement=None,
):
    """Return Commission Entries eligible for settlement for the given beneficiary.

    Only entries that are submitted, not cancelled/reversed, with a positive
    available balance are returned. Each entry is enriched with the balance
    fields used by the Commission Settlement Item child table.
    """
    if not company or not beneficiary_type:
        frappe.throw(_("Company and beneficiary type are required."))

    beneficiary = sales_person if beneficiary_type == "Sales Person" else sales_partner
    if not beneficiary:
        frappe.throw(_("A beneficiary (sales person or sales partner) is required."))

    filters = {
        "docstatus": 1,
        "company": company,
        "beneficiary_type": beneficiary_type,
    }
    if beneficiary_type == "Sales Person":
        filters["sales_person"] = beneficiary
    else:
        filters["sales_partner"] = beneficiary

    if period_from or period_to:
        if period_from and period_to:
            filters["posting_date"] = ["between", [period_from, period_to]]
        elif period_from:
            filters["posting_date"] = [">=", period_from]
        elif period_to:
            filters["posting_date"] = ["<=", period_to]

    # exclude reversed/cancelled entries
    filters["status"] = ["not in", ["Reversed", "Cancelled"]]

    entries = frappe.get_all(
        "Commission Entry",
        filters=filters,
        fields=[
            "name",
            "sales_invoice",
            "posting_date",
            "earned_commission",
            "payable_amount",
            "paid_amount",
            "commission_currency",
            "beneficiary_type",
            "sales_person",
            "sales_partner",
        ],
        order_by="posting_date asc, name asc",
    )

    result = []
    for e in entries:
        if commission_currency and e.get("commission_currency") and \
                e.get("commission_currency") != commission_currency:
            continue
        entry_name = e.get("name")
        earned = flt(e.get("earned_commission"))
        eligible = flt(e.get("payable_amount"))
        paid = flt(e.get("paid_amount"))
        reserved_other = _reserved_for_entry(
            entry_name, exclude_settlement=exclude_settlement
        )
        cap = eligible if eligible > 0 else earned
        available = cap - reserved_other - paid
        if available <= 0.005:
            continue
        result.append(
            {
                "commission_ledger_entry": entry_name,
                "original_source_invoice": e.get("sales_invoice"),
                "beneficiary": (
                    e.get("sales_person") if beneficiary_type == "Sales Person"
                    else e.get("sales_partner")
                ),
                "earned_balance": flt(earned, 2),
                "eligible_balance": flt(cap, 2),
                "reserved_amount": flt(reserved_other, 2),
                "already_settled_amount": flt(paid, 2),
                "amount_included": flt(available, 2),
                "commission_currency": e.get("commission_currency"),
            }
        )
    return result
