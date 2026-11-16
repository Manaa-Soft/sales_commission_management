import frappe
from frappe.model.document import Document
from frappe.utils import flt, now_datetime


class CommissionSettlement(Document):
    def validate(self):
        self._compute_totals()

    def before_submit(self):
        self._compute_totals()
        if flt(self.total_payable) <= 0:
            frappe.throw("Total payable must be greater than zero to submit a settlement.")

    def on_submit(self):
        self.status = "Submitted"
        self.approval_user = frappe.session.user
        self.approval_date = now_datetime()
        self._create_settlement_document()
        self._clear_accrued_liability()
        self.save(ignore_permissions=True)

    def _clear_accrued_liability(self):
        accrued_total = 0.0
        for row in (self.entries or []):
            try:
                entry = frappe.get_doc("Commission Entry", row.commission_ledger_entry)
                accrued_total += flt(getattr(entry, "accrued_amount", 0))
            except Exception:
                continue
        if accrued_total <= 0.005:
            return
        try:
            settings = frappe.get_single("Commission Settings") if frappe.db.exists("Commission Settings") else None
        except Exception:
            settings = None
        accrued_account = None
        payable_account = None
        if settings:
            accrued_account = getattr(settings, "default_accrued_liability_account", None) or None
            payable_account = getattr(settings, "default_commission_payable_account", None) or None
        if not accrued_account or not payable_account:
            frappe.throw(
                _("Accrued liability and commission payable accounts must be configured in Commission Settings before settlement.")
            )
        je = frappe.get_doc({
            "doctype": "Journal Entry",
            "voucher_type": "Journal Entry",
            "company": self.company,
            "posting_date": frappe.utils.nowdate(),
            "accounts": [
                {"account": accrued_account, "debit_in_account_currency": flt(accrued_total)},
                {"account": payable_account, "credit_in_account_currency": flt(accrued_total)},
            ],
            "user_remark": "Clear accrued commission liability for settlement {0}".format(self.name),
        })
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
        for row in (self.entries or []):
            total_earned += flt(row.earned_balance)
            total_payable += flt(row.eligible_balance)
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
            frappe.throw("Employee is required for Additional Salary settlement.")
        if not self.company:
            frappe.throw("Company is required for settlement.")
        salary_component = frappe.db.get_single_value("Commission Settings", "default_salary_component") \
            if frappe.db.exists("Commission Settings") else None
        if not salary_component:
            salary_component = "Commission" if frappe.db.exists("Salary Component", "Commission") else None

        doc = frappe.get_doc({
            "doctype": "Additional Salary",
            "employee": self.employee,
            "salary_component": salary_component,
            "amount": flt(self.total_payable),
            "pay_from_date": self.period_from,
            "pay_to_date": self.period_to,
            "company": self.company,
            "ref_doctype": "Commission Settlement",
            "ref_docname": self.name,
        })
        doc.insert(ignore_permissions=True)
        doc.submit()
        self.settlement_document_type = "Additional Salary"
        self.settlement_document = doc.name

    def _create_purchase_invoice(self):
        if not self.supplier:
            frappe.throw("Supplier is required for Supplier Purchase Invoice settlement.")
        if not self.company:
            frappe.throw("Company is required for settlement.")

        item_code = frappe.db.get_single_value("Commission Settings", "default_commission_item") \
            if frappe.db.exists("Commission Settings") else None
        if not item_code:
            item_code = frappe.db.get_value("Item", {"item_name": "Commission"}, "name")

        doc = frappe.get_doc({
            "doctype": "Purchase Invoice",
            "supplier": self.supplier,
            "company": self.company,
            "posting_date": frappe.utils.nowdate(),
            "items": [{
                "item_code": item_code,
                "qty": 1,
                "rate": flt(self.total_payable),
            }],
            "remarks": "Commission settlement {0}".format(self.name),
        })
        doc.insert(ignore_permissions=True)
        doc.submit()
        self.settlement_document_type = "Purchase Invoice"
        self.settlement_document = doc.name

    def _cancel_settlement_document(self):
        if not self.settlement_document or not self.settlement_document_type:
            return
        try:
            doc = frappe.get_doc(self.settlement_document_type, self.settlement_document)
            if doc.docstatus == 1:
                doc.cancel()
        except Exception:
            frappe.log_error(frappe.get_traceback(), "Commission Settlement cancel failed")
