import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt, now_datetime, getdate


class CommissionAccrualBatch(Document):
    def validate(self):
        self._compute_total()
        self._validate_items()

    def before_submit(self):
        self._compute_total()
        if flt(self.total_amount) <= 0:
            frappe.throw(_("Total accrual amount must be greater than zero."))
        self._validate_no_duplicate_accrual()

    def on_submit(self):
        self.status = "Submitted"
        self._create_journal_entry()
        self.save(ignore_permissions=True)

    def on_cancel(self):
        self._cancel_journal_entry()
        self.status = "Cancelled"
        self.save(ignore_permissions=True)

    def _compute_total(self):
        total = 0.0
        for row in (self.items or []):
            total += flt(row.accrual_amount)
        self.total_amount = flt(total, 2)

    def _validate_items(self):
        seen = set()
        for row in (self.items or []):
            if not row.commission_ledger_entry:
                frappe.throw(_("Commission Ledger Entry is required for each batch item."))
            if row.commission_ledger_entry in seen:
                frappe.throw(_("Duplicate Commission Ledger Entry {0} in batch.").format(row.commission_ledger_entry))
            seen.add(row.commission_ledger_entry)
            if flt(row.accrual_amount) <= 0:
                frappe.throw(_("Accrual amount must be greater than zero for {0}.").format(row.commission_ledger_entry))
            # verify entry is eligible and unaccrued
            entry = frappe.get_doc("Commission Entry", row.commission_ledger_entry)
            if entry.docstatus != 1:
                frappe.throw(_("Commission Entry {0} must be submitted.").format(row.commission_ledger_entry))
            if flt(entry.earned_delta) <= 0:
                frappe.throw(_("Commission Entry {0} has no positive earned amount.").format(row.commission_ledger_entry))
            accrued_to_date = flt(getattr(entry, "accrued_amount", 0))
            eligible = flt(entry.earned_delta) - accrued_to_date
            if flt(row.accrual_amount) > eligible + 0.005:
                frappe.throw(
                    _("Accrual amount {0} exceeds unaccrued balance {1} for {2}.").format(
                        row.accrual_amount, eligible, row.commission_ledger_entry
                    )
                )

    def _validate_no_duplicate_accrual(self):
        # ensure no entry is accrued in another submitted batch
        for row in (self.items or []):
            other = frappe.db.get_value(
                "Commission Accrual Batch Item",
                {"commission_ledger_entry": row.commission_ledger_entry,
                 "parent": ["!=", self.name],
                 "docstatus": 1},
                "parent",
            )
            if other:
                frappe.throw(
                    _("Commission Entry {0} is already accrued in batch {1}.").format(
                        row.commission_ledger_entry, other
                    )
                )

    def _create_journal_entry(self):
        if not self.company:
            frappe.throw(_("Company is required for accrual batch."))
        expense_account = self.expense_account
        liability_account = self.accrued_liability_account
        if not expense_account:
            expense_account = frappe.db.get_single_value("Commission Settings", "default_commission_expense_account") \
                if frappe.db.exists("Commission Settings") else None
        if not liability_account:
            liability_account = frappe.db.get_single_value("Commission Settings", "default_accrued_liability_account") \
                if frappe.db.exists("Commission Settings") else None
        if not expense_account or not liability_account:
            frappe.throw(_("Commission expense and accrued liability accounts must be configured."))

        je = frappe.get_doc({
            "doctype": "Journal Entry",
            "voucher_type": "Journal Entry",
            "company": self.company,
            "posting_date": self.accrual_date or frappe.utils.nowdate(),
            "accounts": [
                {
                    "account": expense_account,
                    "debit_in_account_currency": flt(self.total_amount),
                    "cost_center": self.cost_center,
                },
                {
                    "account": liability_account,
                    "credit_in_account_currency": flt(self.total_amount),
                    "cost_center": self.cost_center,
                },
            ],
            "user_remark": "Commission accrual batch {0}".format(self.name),
        })
        je.insert(ignore_permissions=True)
        je.submit()
        self.journal_entry = je.name

        # update accrued amount on each entry
        for row in (self.items or []):
            entry = frappe.get_doc("Commission Entry", row.commission_ledger_entry)
            current = flt(getattr(entry, "accrued_amount", 0))
            entry.accrued_amount = flt(current + row.accrual_amount, 6)
            entry.accrual_batch = self.name
            entry.save(ignore_permissions=True)

    def _cancel_journal_entry(self):
        if not getattr(self, "journal_entry", None):
            return
        try:
            je = frappe.get_doc("Journal Entry", self.journal_entry)
            if je.docstatus == 1:
                je.cancel()
        except Exception:
            frappe.log_error(frappe.get_traceback(), "Commission Accrual Batch cancel failed")
        # restore accrued amounts
        for row in (self.items or []):
            try:
                entry = frappe.get_doc("Commission Entry", row.commission_ledger_entry)
                current = flt(getattr(entry, "accrued_amount", 0))
                entry.accrued_amount = flt(current - row.accrual_amount, 6)
                if entry.accrued_amount <= 0.005:
                    entry.accrued_amount = 0
                    entry.accrual_batch = None
                entry.save(ignore_permissions=True)
            except Exception:
                frappe.log_error(frappe.get_traceback(), "Accrual reversal failed for {0}".format(row.commission_ledger_entry))
