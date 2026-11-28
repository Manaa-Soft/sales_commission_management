import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, flt, nowdate


def _company_currency(company):
    return (
        frappe.get_cached_value("Company", company, "default_currency")
        if company
        else None
    )


def _accrued_for_entry(entry_name, exclude_batch=None, for_update=False, use_company_amount=False):
    """Sum of accrual amounts already posted for an entry by submitted batches.

    Uses submitted Commission Accrual Batch Items as the allocation ledger,
    so an entry can be accrued across multiple batches.

    If use_company_amount is True, sums company_amount (company currency)
    instead of accrual_amount (commission currency). Used for GL reversals.
    """
    total = 0.0
    if for_update:
        field_name = "company_amount" if use_company_amount else "accrual_amount"
        q = frappe.qb.from_("Commission Accrual Batch Item").select(
            field_name, "parent"
        )
        q = q.where(frappe.qb.Field("commission_ledger_entry") == entry_name)
        q = q.where(frappe.qb.Field("docstatus") == 1)
        if exclude_batch:
            q = q.where(frappe.qb.Field("parent") != exclude_batch)
        items = q.for_update().run(as_dict=True)
    else:
        filters = {"commission_ledger_entry": entry_name, "docstatus": 1}
        if exclude_batch:
            filters["parent"] = ["!=", exclude_batch]
        field_name = "company_amount" if use_company_amount else "accrual_amount"
        items = frappe.get_all(
            "Commission Accrual Batch Item",
            filters=filters,
            fields=[field_name, "parent"],
        )
    if not items:
        return flt(total, 6)
    if for_update:
        # Under locking reads, Frappe syncs parent docstatus into child rows,
        # so the docstatus=1 filter on child rows is sufficient. Sum directly.
        for i in items or []:
            total += flt(i.get(field_name))
    else:
        # Non-locking preview path: verify parent batch is submitted.
        parents = frappe.get_all(
            "Commission Accrual Batch",
            filters={"name": ["in", [i["parent"] for i in items]], "docstatus": 1},
            pluck="name",
        )
        submitted_parents = set(parents or [])
        for i in items or []:
            if i.get("parent") in submitted_parents:
                total += flt(i.get(field_name))
    return flt(total, 6)


def _accrued_for_entry_by_account(entry_name, exclude_batch=None, use_company_amount=True):
    """Return a dict mapping accrued liability account -> amount for an entry.

    Uses submitted Commission Accrual Batch Items, so an entry accrued across
    multiple batches/accounts is fully represented. Amounts are in company
    currency when use_company_amount is True.
    """
    field_name = "company_amount" if use_company_amount else "accrual_amount"
    filters = {"commission_ledger_entry": entry_name, "docstatus": 1}
    if exclude_batch:
        filters["parent"] = ["!=", exclude_batch]
    items = frappe.get_all(
        "Commission Accrual Batch Item",
        filters=filters,
        fields=[field_name, "parent", "accrued_liability_account"],
    )
    if not items:
        return {}
    # Validate parent batches and their linked Journal Entries.
    batch_names = list({i.get("parent") for i in items if i.get("parent")})
    batches = frappe.get_all(
        "Commission Accrual Batch",
        filters={"name": ["in", batch_names]},
        fields=["name", "docstatus", "journal_entry", "company", "accrual_date"],
    )
    batch_map = {b.get("name"): b for b in (batches or [])}
    submitted = set()
    for b in batches or []:
        if flt(b.get("docstatus")) != 1:
            continue
        je_name = b.get("journal_entry")
        if je_name:
            je_status = frappe.get_cached_value("Journal Entry", je_name, "docstatus")
            if je_status != 1:
                continue
            je_company = frappe.get_cached_value("Journal Entry", je_name, "company")
            if je_company and je_company != b.get("company"):
                continue
        else:
            # Batch has no linked JE: cannot validate GL; skip to avoid
            # counting unposted accruals.
            continue
        submitted.add(b.get("name"))
    result = {}
    for i in items or []:
        if i.get("parent") not in submitted:
            continue
        acct = i.get("accrued_liability_account")
        if not acct:
            continue
        result[acct] = flt(result.get(acct, 0.0)) + flt(i.get(field_name))
    return result

def _reclassified_for_entry(entry_name, exclude_settlement=None):
    """Sum of accrued liability already reclassified to payable by other settlements."""
    total = 0.0
    sett_filters = {"docstatus": 1}
    if exclude_settlement:
        sett_filters["name"] = ["!=", exclude_settlement]
    settlements = frappe.get_all(
        "Commission Settlement",
        filters=sett_filters,
        fields=["name", "settlement_journal_entry"],
    )
    if not settlements:
        return total
    valid_names = []
    for s in settlements:
        je_name = getattr(s, "settlement_journal_entry", None)
        if je_name:
            je_status = frappe.get_cached_value(
                "Journal Entry", je_name, "docstatus"
            )
            if je_status != 1:
                continue
        else:
            continue
        valid_names.append(s.name)
    if not valid_names:
        return total
    items = frappe.get_all(
        "Commission Settlement Item",
        filters={
            "commission_ledger_entry": entry_name,
            "parent": ["in", valid_names],
        },
        fields=["amount_included", "reclassified_amount"],
    )
    for r in items or []:
        ra = r.get("reclassified_amount")
        if ra is None or ra == "":
            total += flt(r.get("amount_included"))
        else:
            total += flt(ra)
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
        self._create_journal_entry()
        update = {"status": "Submitted"}
        if getattr(self, "journal_entry", None):
            update["journal_entry"] = self.journal_entry
        frappe.db.set_value(
            "Commission Accrual Batch", self.name, update, update_modified=False
        )

    def on_cancel(self):
        self._cancel_journal_entry()
        frappe.db.set_value(
            "Commission Accrual Batch", self.name,
            {"status": "Cancelled"}, update_modified=False
        )

    def _set_defaults(self):
        if not self.company:
            return
        # Always derive accounting_currency from the company (server-side).
        self.accounting_currency = _company_currency(self.company)
        if not self.accrual_date:
            self.accrual_date = self.period_to or nowdate()
        # Resolve accounts from company-specific mapping first.
        accrued = None
        expense = None
        cost_center = None
        try:
            settings = (
                frappe.get_single("Commission Settings")
                if frappe.db.exists("DocType", "Commission Settings")
                else None
            )
        except Exception:
            settings = None
        if settings:
            for row in (getattr(settings, "account_mappings", None) or []):
                if getattr(row, "company", None) == self.company and cint(getattr(row, "enabled", 1)):
                    accrued = getattr(row, "accrued_commission_payable_account", None) or accrued
                    expense = getattr(row, "commission_expense_account", None) or expense
                    cost_center = getattr(row, "cost_center", None) or cost_center
                    break
            if not expense:
                expense = getattr(settings, "default_commission_expense_account", None)
            if not accrued:
                accrued = getattr(settings, "default_accrued_liability_account", None)
            if not cost_center:
                cost_center = getattr(settings, "default_cost_center", None)
        if not self.expense_account:
            self.expense_account = expense
        if not self.accrued_liability_account:
            self.accrued_liability_account = accrued
        if not self.cost_center:
            self.cost_center = cost_center

    def _compute_company_amounts(self):
        currencies = set()
        for row in self.items or []:
            if not row.commission_ledger_entry:
                continue
            # Fail closed: entry must exist.
            entry = frappe.get_doc(
                "Commission Entry", row.commission_ledger_entry, for_update=True
            )
            # Validate entry belongs to this batch's company.
            if getattr(entry, "company", None) and entry.company != self.company:
                frappe.throw(
                    _("Commission Entry {0} does not belong to company {1}.").format(
                        row.commission_ledger_entry, self.company
                    )
                )
            # Validate entry company currency matches batch accounting currency.
            if getattr(entry, "company_currency", None) and entry.company_currency != self.accounting_currency:
                frappe.throw(
                    _("Commission Entry {0} company currency {1} does not match batch accounting currency {2}.").format(
                        row.commission_ledger_entry, entry.company_currency, self.accounting_currency
                    )
                )
            # Always derive commission_currency from the linked entry (server-side).
            row.commission_currency = getattr(entry, "commission_currency", None)
            currencies.add(row.commission_currency)
            # Prefer the dedicated commission-to-company rate; fall back to the
            # invoice conversion rate only when commission currency matches the
            # transaction currency.
            import math
            c2c_rate = flt(getattr(entry, "commission_to_company_rate", 0))
            # Validate non-finite or negative stored rates.
            if c2c_rate and (not math.isfinite(c2c_rate) or c2c_rate <= 0):
                frappe.throw(
                    _("Invalid commission-to-company exchange rate for {0}.").format(
                        row.commission_ledger_entry
                    )
                )
            if not c2c_rate:
                comm_curr = getattr(entry, "commission_currency", None)
                txn_curr = getattr(entry, "transaction_currency", None)
                comp_curr = getattr(entry, "company_currency", None)
                if comm_curr and comp_curr and comm_curr == comp_curr:
                    # Identical currencies: rate is legitimately 1.0.
                    c2c_rate = 1.0
                elif comm_curr and txn_curr and comm_curr == txn_curr:
                    snapshot = flt(getattr(entry, "conversion_rate_snapshot", 0))
                    if not snapshot or not math.isfinite(snapshot) or snapshot <= 0:
                        frappe.throw(
                            _("Missing commission-to-company exchange rate for {0}.").format(
                                row.commission_ledger_entry
                            )
                        )
                    c2c_rate = snapshot
                else:
                    # No valid rate available; reject the batch rather than
                    # substituting a foreign amount as company currency.
                    frappe.throw(
                        _("Missing commission-to-company exchange rate for {0}.").format(
                            row.commission_ledger_entry
                        )
                    )
            # Always overwrite conversion_rate with the authoritative rate used.
            row.conversion_rate = c2c_rate
            # commission -> company conversion
            if row.commission_currency == self.accounting_currency:
                row.company_amount = flt(row.accrual_amount, 6)
            else:
                if not c2c_rate or c2c_rate <= 0 or not math.isfinite(c2c_rate):
                    frappe.throw(
                        _("Missing commission-to-company exchange rate for {0}.").format(
                            row.commission_ledger_entry
                        )
                    )
                row.company_amount = flt(flt(row.accrual_amount) * c2c_rate, 6)
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
            company_amount = getattr(row, "company_amount", None)
            if company_amount is None:
                frappe.throw(
                    _("Company amount is not set for Commission Entry {0}.").format(
                        row.commission_ledger_entry
                    )
                )
            total += flt(company_amount)
        from sales_commission_management.sales_commission_management.utils import currency_precision
        prec = currency_precision(self.accounting_currency)
        self.total_amount = flt(total, prec)

    def _validate_items(self):
        seen = set()
        # Lock rows in a consistent order to prevent concurrent accrual batches
        # from calculating the same unaccrued balance.
        entry_names = sorted(
            {row.commission_ledger_entry for row in (self.items or []) if row.commission_ledger_entry}
        )
        # Use Frappe Query Builder for portable row locking (for_update=True).
        for en in entry_names:
            frappe.qb.from_("Commission Entry").select("name").where(
                frappe.qb.Field("name") == en
            ).for_update().run()
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
            # For Return Adjustment entries, the accrued_amount is the authoritative
            # accrual-reversal basis, including when it is zero: a return with no
            # previously accrued amount must not generate an accrual reversal.
            entry_accrued = flt(getattr(entry, "accrued_amount", 0))
            is_return = getattr(entry, "event_type", None) == "Return Adjustment"
            if is_return:
                earned = entry_accrued
            elif entry_accrued != 0 and abs(entry_accrued) < abs(earned):
                earned = entry_accrued
            accrued = _accrued_for_entry(
                row.commission_ledger_entry, exclude_batch=self.name, for_update=True
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
            raw_amt = getattr(row, "company_amount", None)
            if raw_amt is None:
                frappe.throw(
                    _("Company amount is not set for {0}.").format(row.commission_ledger_entry)
                )
            amt = flt(raw_amt)
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
            from sales_commission_management.sales_commission_management.utils import currency_precision
            prec = currency_precision(self.accounting_currency)
            if kind == "debit":
                row["debit_in_account_currency"] = flt(amt, prec)
            else:
                row["credit_in_account_currency"] = flt(amt, prec)
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
        # Fail closed: if the Journal Entry cannot be cancelled, abort batch
        # cancellation so the subledger and GL stay synchronized.
        je = frappe.get_doc("Journal Entry", self.journal_entry)
        if je.docstatus == 1:
            je.cancel()


@frappe.whitelist()
def fetch_unaccrued_entries(company, period_from, period_to, exclude_batch=None):
    """Return submitted Commission Entries with unaccrued balance within period."""
    if frappe.session.user == "Guest":
        frappe.throw(_("Authentication required."), frappe.PermissionError)

    privileged = {"System Manager", "Accounts Manager", "Sales Manager"}
    user_roles = set(frappe.get_roles(frappe.session.user))
    if not (privileged & user_roles):
        frappe.throw(_("Insufficient permissions for accrual operations."), frappe.PermissionError)

    if not company or not period_from or not period_to:
        frappe.throw(_("Company and period are required."))

    # Verify company access.
    user_companies = frappe.get_all("User Permission", filters={"user": frappe.session.user, "allow": "Company"}, pluck="for_value")
    if not user_companies:
        frappe.throw(_("No company access configured for current user."), frappe.PermissionError)
    if company not in user_companies:
        frappe.throw(_("No access to company {0}.").format(company), frappe.PermissionError)
    from frappe.utils import getdate

    entries = frappe.get_all(
        "Commission Entry",
        filters={
            "docstatus": 1,
            "company": company,
            "posting_date": ["between", [period_from, period_to]],
            "event_type": ["in", ["Invoice Earned", "Return Adjustment"]],
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
        # For Return Adjustment entries, the accrued_amount is the authoritative
        # accrual-reversal basis, including when it is zero.
        entry_accrued = flt(e.get("accrued_amount", 0))
        if e.get("event_type") == "Return Adjustment":
            earned = entry_accrued
        elif entry_accrued != 0 and abs(entry_accrued) < abs(earned):
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
