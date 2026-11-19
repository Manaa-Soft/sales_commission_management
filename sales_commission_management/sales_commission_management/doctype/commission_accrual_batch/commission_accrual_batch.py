import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt, nowdate


def _company_currency(company):
    return (
        frappe.get_cached_value("Company", company, "default_currency")
        if company
        else None
    )


def _accrued_for_entry(entry_name, exclude_batch=None):
    """Sum of accrual amounts already posted for an entry by submitted batches.

    Uses submitted Commission Accrual Batch Items as the allocation ledger,
    so an entry can be accrued across multiple batches.
    """
    total = 0.0
    try:
        filters = {"commission_ledger_entry": entry_name, "docstatus": 1}
        if exclude_batch:
            filters["parent"] = ["!=", exclude_batch]
        items = frappe.get_all(
            "Commission Accrual Batch Item",
            filters=filters,
            fields=["accrual_amount", "parent"],
        )
    except Exception:
        items = []
    # Only count items whose parent batch is submitted.
    submitted_parents = set()
    try:
        parents = frappe.get_all(
            "Commission Accrual Batch",
            filters={"name": ["in", [i["parent"] for i in items]] , "docstatus": 1},
            pluck="name",
        )
        submitted_parents = set(parents or [])
    except Exception:
        submitted_parents = {i["parent"] for i in items}
    for i in items or []:
        if i.get("parent") in submitted_parents:
            total += flt(i.get("accrual_amount"))
    # If excluding a batch, we already filtered parent != exclude_batch above.
    return flt(total, 6)


def _reclassified_for_entry(entry_name, exclude_settlement=None):
    """Sum of accrued liability already reclassified to payable by other settlements."""
    total = 0.0
    try:
        sett_filters = {"docstatus": 1}
        if exclude_settlement:
            sett_filters["name"] = ["!=", exclude_settlement]
        settlements = frappe.get_all(
            "Commission Settlement",
            filters=sett_filters,
            fields=["name", "settlement_journal_entry"],
        )
    except Exception:
        settlements = []
    if not settlements:
        return total
    valid_names = []
    for s in settlements:
        je_name = getattr(s, "settlement_journal_entry", None)
        if je_name:
            try:
                je_status = frappe.get_cached_value(
                    "Journal Entry", je_name, "docstatus"
                )
            except Exception:
                je_status = None
            if je_status != 1:
                continue
        else:
            continue
        valid_names.append(s.name)
    if not valid_names:
        return total
    try:
        items = frappe.get_all(
            "Commission Settlement Item",
            filters={
                "commission_ledger_entry": entry_name,
                "parent": ["in", valid_names],
            },
            fields=["amount_included"],
        )
    except Exception:
        items = []
    for r in items or []:
        total += flt(r.get("amount_included"))
    return flt(total, 6)


class CommissionAccrualBatch(Document):
    def validate(self):
        self._set_defaults()
        self._compute_company_amounts()
        self._compute_total()
        self._validate_items()

    def before_submit(self):
        self._set_defaults()
        self._compute_company_amounts()
        self._compute_total()
        if abs(flt(self.total_amount)) <= 0.005:
            frappe.throw(_("Total accrual amount must be non-zero."))
        self._validate_items()

    def on_submit(self):
        self.status = "Submitted"
        self._create_journal_entry()
        self.save(ignore_permissions=True)

    def on_cancel(self):
        self._cancel_journal_entry()
        self.status = "Cancelled"
        self.save(ignore_permissions=True)

    def _set_defaults(self):
        if not self.company:
            return
        if not self.accounting_currency:
            self.accounting_currency = _company_currency(self.company)
        if not self.accrual_date:
            self.accrual_date = self.period_to or nowdate()
        if not self.expense_account:
            try:
                self.expense_account = frappe.db.get_single_value(
                    "Commission Settings", "default_commission_expense_account"
                )
            except Exception:
                self.expense_account = None
        if not self.accrued_liability_account:
            try:
                self.accrued_liability_account = frappe.db.get_single_value(
                    "Commission Settings", "default_accrued_liability_account"
                )
            except Exception:
                self.accrued_liability_account = None
        if not self.cost_center:
            try:
                self.cost_center = frappe.db.get_single_value(
                    "Commission Settings", "default_cost_center"
                )
            except Exception:
                self.cost_center = None

    def _compute_company_amounts(self):
        currencies = set()
        for row in self.items or []:
            if not row.commission_ledger_entry:
                continue
            try:
                entry = frappe.get_doc("Commission Entry", row.commission_ledger_entry)
            except Exception:
                continue
            if not getattr(row, "commission_currency", None):
                row.commission_currency = getattr(entry, "commission_currency", None)
            currencies.add(row.commission_currency)
            rate = flt(getattr(entry, "conversion_rate_snapshot", 1)) or 1.0
            if not getattr(row, "conversion_rate", None):
                row.conversion_rate = rate
            # commission -> company conversion
            if row.commission_currency == self.accounting_currency:
                row.company_amount = flt(row.accrual_amount, 6)
            else:
                # commission currency equals transaction currency in the common case
                row.company_amount = flt(flt(row.accrual_amount) * rate, 6)
        # enforce single currency per batch
        if currencies and None not in currencies and len(currencies) > 1:
            frappe.throw(
                _("All batch items must share the same commission currency.")
            )
        if currencies:
            self.commission_currency = next(iter(c for c in currencies if c))

    def _compute_total(self):
        total = 0.0
        for row in self.items or []:
            total += flt(getattr(row, "company_amount", None) or row.accrual_amount)
        self.total_amount = flt(total, 2)

    def _validate_items(self):
        seen = set()
        for row in self.items or []:
            if not row.commission_ledger_entry:
                frappe.throw(_("Commission Ledger Entry is required for each batch item."))
            if row.commission_ledger_entry in seen:
                frappe.throw(
                    _("Duplicate Commission Ledger Entry {0} in batch.").format(
                        row.commission_ledger_entry
                    )
                )
            seen.add(row.commission_ledger_entry)

            amt = flt(row.accrual_amount)
            if amt == 0:
                frappe.throw(
                    _("Accrual amount cannot be zero for {0}.").format(
                        row.commission_ledger_entry
                    )
                )

            entry = frappe.get_doc("Commission Entry", row.commission_ledger_entry)
            if entry.docstatus != 1:
                frappe.throw(
                    _("Commission Entry {0} must be submitted.").format(
                        row.commission_ledger_entry
                    )
                )
            if entry.company != self.company:
                frappe.throw(
                    _("Commission Entry {0} belongs to a different company.").format(
                        row.commission_ledger_entry
                    )
                )
            # period enforcement
            if self.period_from and self.period_to:
                pd = getattr(entry, "posting_date", None)
                if pd:
                    from frappe.utils import getdate
                    if getdate(pd) < getdate(self.period_from) or getdate(pd) > getdate(self.period_to):
                        frappe.throw(
                            _(
                                "Commission Entry {0} posting date is outside the batch period."
                            ).format(row.commission_ledger_entry)
                        )

            earned = flt(getattr(entry, "earned_delta", 0))
            # For Return Adjustment entries, cap the accrual basis to the
            # entry's accrued_amount so we never reverse more than actually accrued.
            entry_accrued = flt(getattr(entry, "accrued_amount", 0))
            if entry_accrued != 0 and abs(entry_accrued) < abs(earned):
                earned = entry_accrued
            accrued = _accrued_for_entry(
                row.commission_ledger_entry, exclude_batch=self.name
            )
            unaccrued = earned - accrued
            # sign consistency
            if unaccrued >= 0 and amt < 0:
                frappe.throw(
                    _("Accrual amount must be positive for {0}.").format(
                        row.commission_ledger_entry
                    )
                )
            if unaccrued < 0 and amt > 0:
                frappe.throw(
                    _("Accrual amount must be negative for {0}.").format(
                        row.commission_ledger_entry
                    )
                )
            if unaccrued >= 0 and amt > unaccrued + 0.005:
                frappe.throw(
                    _(
                        "Accrual amount {0} exceeds unaccrued balance {1} for {2}."
                    ).format(amt, unaccrued, row.commission_ledger_entry)
                )
            if unaccrued < 0 and amt < unaccrued - 0.005:
                frappe.throw(
                    _(
                        "Accrual amount {0} exceeds unaccrued balance {1} for {2}."
                    ).format(amt, unaccrued, row.commission_ledger_entry)
                )

            # row-level account defaults from parent
            if not getattr(row, "commission_expense_account", None):
                row.commission_expense_account = self.expense_account
            if not getattr(row, "accrued_liability_account", None):
                row.accrued_liability_account = self.accrued_liability_account
            if not getattr(row, "cost_center", None):
                row.cost_center = self.cost_center

    def _create_journal_entry(self):
        if not self.company:
            frappe.throw(_("Company is required for accrual batch."))
        # Build per-row GL lines, grouping by (expense, liability, cost_center, project)
        lines = []
        for row in self.items or []:
            amt = flt(getattr(row, "company_amount", None) or row.accrual_amount)
            if amt == 0:
                continue
            expense_account = getattr(row, "commission_expense_account", None) or self.expense_account
            liability_account = getattr(row, "accrued_liability_account", None) or self.accrued_liability_account
            if not expense_account or not liability_account:
                frappe.throw(
                    _("Commission expense and accrued liability accounts must be configured.")
                )
            cc = getattr(row, "cost_center", None) or self.cost_center
            proj = getattr(row, "project", None)
            # debit expense, credit liability for positive; reverse for negative
            if amt > 0:
                lines.append(("debit", expense_account, amt, cc, proj))
                lines.append(("credit", liability_account, amt, cc, proj))
            else:
                a = abs(amt)
                lines.append(("credit", expense_account, a, cc, proj))
                lines.append(("debit", liability_account, a, cc, proj))

        if not lines:
            frappe.throw(_("No accrual lines to post."))

        # aggregate into accounts list
        agg = {}
        for kind, acct, amt, cc, proj in lines:
            key = (kind, acct, cc or "", proj or "")
            agg[key] = agg.get(key, 0.0) + amt

        accounts = []
        for (kind, acct, cc, proj), amt in agg.items():
            row = {"account": acct}
            if kind == "debit":
                row["debit_in_account_currency"] = flt(amt, 2)
            else:
                row["credit_in_account_currency"] = flt(amt, 2)
            if cc:
                row["cost_center"] = cc
            if proj:
                row["project"] = proj
            accounts.append(row)

        # ensure balanced
        debits = sum(flt(a.get("debit_in_account_currency", 0)) for a in accounts)
        credits = sum(flt(a.get("credit_in_account_currency", 0)) for a in accounts)
        if abs(debits - credits) > 0.01:
            frappe.throw(
                _("Journal Entry is not balanced: debit {0} vs credit {1}.").format(
                    debits, credits
                )
            )

        je = frappe.get_doc(
            {
                "doctype": "Journal Entry",
                "voucher_type": "Journal Entry",
                "company": self.company,
                "posting_date": self.accrual_date or nowdate(),
                "accounts": accounts,
                "user_remark": f"Commission accrual batch {self.name}",
            }
        )
        je.insert(ignore_permissions=True)
        je.submit()
        self.journal_entry = je.name

    def _cancel_journal_entry(self):
        if not getattr(self, "journal_entry", None):
            return
        try:
            je = frappe.get_doc("Journal Entry", self.journal_entry)
            if je.docstatus == 1:
                je.cancel()
        except Exception:
            frappe.log_error(
                frappe.get_traceback(), "Commission Accrual Batch cancel failed"
            )


@frappe.whitelist()
def fetch_unaccrued_entries(company, period_from, period_to, exclude_batch=None):
    """Return submitted Commission Entries with unaccrued balance within period."""
    if not company or not period_from or not period_to:
        frappe.throw(_("Company and period are required."))
    from frappe.utils import getdate

    entries = frappe.get_all(
        "Commission Entry",
        filters={
            "docstatus": 1,
            "company": company,
            "posting_date": ["between", [period_from, period_to]],
            "status": ["not in", ["Reversed", "Cancelled"]],
        },
        fields=[
            "name",
            "sales_invoice",
            "posting_date",
            "beneficiary_type",
            "sales_person",
            "sales_partner",
            "earned_delta",
            "accrued_amount",
            "event_type",
            "commission_currency",
            "conversion_rate_snapshot",
        ],

        order_by="posting_date asc, name asc",
    )

    result = []
    for e in entries:
        earned = flt(e.get("earned_delta"))
        # For Return Adjustment entries, the accrual reversal is capped at the
        # entry's accrued_amount (the actually accrued portion of the original),
        # never the full earned clawback.
        entry_accrued = flt(e.get("accrued_amount", 0))
        if entry_accrued != 0 and abs(entry_accrued) < abs(earned):
            earned = entry_accrued
        accrued = _accrued_for_entry(e.get("name"), exclude_batch=exclude_batch)
        unaccrued = earned - accrued
        if abs(unaccrued) <= 0.005:
            continue
        result.append(
            {
                "commission_ledger_entry": e.get("name"),
                "commission_currency": e.get("commission_currency"),
                "accrual_amount": flt(unaccrued, 6),
                "conversion_rate": flt(e.get("conversion_rate_snapshot") or 1),
                "beneficiary": e.get("sales_person") or e.get("sales_partner"),
                "posting_date": e.get("posting_date"),
                "sales_invoice": e.get("sales_invoice"),
            }
        )
    return result
