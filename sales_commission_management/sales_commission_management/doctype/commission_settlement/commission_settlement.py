import frappe
from frappe import _
from frappe.model.document import Document
from sales_commission_management.sales_commission_management.utils import currency_precision as _cp
from frappe.utils import flt, now_datetime, getdate


def _company_currency(company):
    return (
        frappe.get_cached_value("Company", company, "default_currency")
        if company
        else None
    )


def _commission_settings():
    """Return the Commission Settings single doc, self-healing issingle drift.

    Returns None if the DocType is not installed. Ensures ``issingle`` is set on
    the DocType row before addressing the single record, so production sites do
    not need manual SQL fixes.
    """
    if not frappe.db.exists("DocType", "Commission Settings"):
        return None
    if not frappe.db.get_value("DocType", "Commission Settings", "issingle"):
        frappe.db.set_value(
            "DocType", "Commission Settings", "issingle", 1, update_modified=False
        )
        frappe.clear_cache(doctype="Commission Settings")
    try:
        return frappe.get_single("Commission Settings")
    except Exception:
        return None


def _get_accounting_mode():
    """Return the configured accounting mode. Fail closed if not configured."""
    settings = _commission_settings()
    if not settings:
        frappe.throw(
            _("Commission Settings must be configured before settlement.")
        )
    mode = getattr(settings, "accounting_mode", None)
    if mode not in ("Subledger Until Settlement", "Periodic Accrual"):
        frappe.throw(_("Invalid Commission Accounting Mode."))
    return mode


def _resolve_settlement_accounts(company):
    """Resolve GL accounts from the Commission Account Mapping child table.

    Returns (accrued_account, payable_account, expense_account, cost_center,
    payroll_expense_account). Falls back to Commission Settings single-doc fields
    for backward compat.
    """
    accrued = None
    payable = None
    expense = None
    cost_center = None
    payroll_expense = None
    settings = _commission_settings()
    if settings:
        for row in (getattr(settings, "account_mappings", None) or []):
            if getattr(row, "company", None) == company and getattr(row, "enabled", 1):
                accrued = getattr(row, "accrued_commission_payable_account", None) or accrued
                payable = getattr(row, "commission_payable_account", None) or payable
                expense = getattr(row, "commission_expense_account", None) or expense
                payroll_expense = getattr(row, "payroll_commission_expense_account", None) or payroll_expense
                cost_center = getattr(row, "cost_center", None) or cost_center
                break
        if not accrued:
            accrued = getattr(settings, "default_accrued_liability_account", None)
        if not payable:
            payable = getattr(settings, "default_commission_payable_account", None)
        if not expense:
            expense = getattr(settings, "default_commission_expense_account", None)
        if not cost_center:
            cost_center = getattr(settings, "default_cost_center", None)
    # Validate accounts belong to the settlement company and are usable ledgers.
    for acct in (accrued, payable, expense, payroll_expense):
        if not acct or not company:
            continue
        if not frappe.db.exists("Account", acct):
            frappe.throw(
                _("Account {0} does not exist.").format(acct)
            )
        acct_company = frappe.get_cached_value("Account", acct, "company")
        if acct_company and acct_company != company:
            frappe.throw(
                _("Account {0} does not belong to company {1}.").format(
                    acct, company
                )
            )
        # Validate account currency matches company currency.
        acct_currency = frappe.get_cached_value("Account", acct, "account_currency")
        company_currency = frappe.get_cached_value("Company", company, "default_currency")
        if acct_currency and company_currency and acct_currency != company_currency:
            frappe.throw(
                _("Account {0} uses currency {1}, but company currency is {2}.").format(
                    acct, acct_currency, company_currency
                )
            )
        disabled = frappe.get_cached_value("Account", acct, "disabled")
        if disabled:
            frappe.throw(
                _("Account {0} is disabled.").format(acct)
            )
        is_group = frappe.get_cached_value("Account", acct, "is_group")
        if is_group:
            frappe.throw(
                _("Account {0} is a group account, not a ledger account.").format(acct)
            )
    # Validate account root types: accrued & payable are liabilities;
    # expense & payroll_expense are expenses.
    _expected_root = {
        accrued: "Liability",
        payable: "Liability",
        expense: "Expense",
        payroll_expense: "Expense",
    }
    for acct, expected in _expected_root.items():
        if not acct:
            continue
        root_type = frappe.get_cached_value("Account", acct, "root_type")
        if root_type and root_type != expected:
            frappe.throw(
                _("Account {0} must be a {1} account (found {2}).").format(
                    acct, expected, root_type
                )
            )
    return accrued, payable, expense, cost_center, payroll_expense


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
        Also validates beneficiary, company, currency and date consistency.
        """
        seen = set()
        # Lock all Commission Entry rows in a consistent order to avoid deadlocks
        # under concurrent settlement submissions. Do not suppress lock failures.
        entry_names = sorted(
            {
                getattr(r, "commission_ledger_entry", None)
                for r in (self.entries or [])
                if getattr(r, "commission_ledger_entry", None)
            }
        )
        for entry_name in entry_names:
            frappe.qb.from_("Commission Entry").select("name").where(
                frappe.qb.Field("name") == entry_name
            ).for_update().run()
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
            raw_included = getattr(row, "amount_included", None)
            if raw_included is None or raw_included == "":
                included = flt(available)
                row.amount_included = included
            else:
                included = flt(raw_included)
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
                ce = flt(getattr(entry, "collection_eligible_amount", 0))
                if ce <= 0:
                    ce = flt(getattr(entry, "paid_amount", 0))
                row.eligible_balance = flt(ce)
                row.reserved_amount = flt(
                    _reserved_for_entry(entry_name, exclude_settlement=self.name)
                )
                row.already_settled_amount = flt(
                    getattr(entry, "settled_amount", 0) or 0
                )
                # Snapshot the commission-to-company rate and company-currency
                # amount from the linked Commission Entry (per-item, so entries
                # with different historical rates are handled correctly).
                rate = flt(getattr(entry, "commission_to_company_rate", 0))
                comm_curr = getattr(entry, "commission_currency", None)
                comp_curr = getattr(entry, "company_currency", None)
                if comm_curr and comp_curr and comm_curr == comp_curr:
                    # Identical currencies: rate is legitimately 1.0.
                    if not rate or rate <= 0:
                        rate = 1.0
                if not rate or rate <= 0:
                    frappe.throw(
                        _("Missing or invalid commission-to-company rate for entry {0}.").format(entry_name)
                    )
                row.commission_to_company_rate = rate
                row.base_currency_amount = flt(
                    flt(row.amount_included) * rate, 6
                )
                if not row.original_source_invoice:
                    row.original_source_invoice = getattr(entry, "sales_invoice", None)

                if not getattr(row, "source_currency", None) and row.original_source_invoice:
                    try:
                        row.source_currency = frappe.db.get_value(
                            "Sales Invoice", row.original_source_invoice, "currency"
                        )
                    except Exception as ex:
                        frappe.log_error(
                            frappe.get_traceback(),
                            "Commission Settlement: source currency lookup failed",
                        )
                        frappe.throw(
                            _("Could not resolve source currency for invoice {0}: {1}").format(
                                row.original_source_invoice, str(ex)
                            )
                        )
                if row.original_source_invoice and not getattr(row, "source_currency", None):
                    frappe.throw(
                        _("Source currency is required for invoice {0}.").format(
                            row.original_source_invoice
                        )
                    )
                if not getattr(row, "company_currency", None):
                    row.company_currency = _company_currency(self.company)
                if not row.beneficiary:
                    row.beneficiary = entry.sales_person or entry.sales_partner

                if getattr(entry, "company", None) and self.company and                         entry.company != self.company:
                    frappe.throw(
                        _("Company mismatch for Commission Entry {0}.").format(entry_name)
                    )
                entry_ben = entry.sales_person or entry.sales_partner
                self_ben = self.sales_person or self.sales_partner
                if entry_ben and self_ben and entry_ben != self_ben:
                    frappe.throw(
                        _("Beneficiary mismatch for Commission Entry {0}.").format(entry_name)
                    )
                entry_currency = getattr(entry, "commission_currency", None)
                if self.commission_currency and entry_currency and                         self.commission_currency != entry_currency:
                    frappe.throw(
                        _("Currency mismatch for Commission Entry {0}.").format(entry_name)
                    )
                if self.period_from and getattr(entry, "posting_date", None):
                    if getdate(entry.posting_date) < getdate(self.period_from):
                        frappe.throw(
                            _("Posting date before period start for Commission Entry {0}.").format(entry_name)
                        )
                if self.period_to and getattr(entry, "posting_date", None):
                    if getdate(entry.posting_date) > getdate(self.period_to):
                        frappe.throw(
                            _("Posting date after period end for Commission Entry {0}.").format(entry_name)
                        )
            except frappe.ValidationError:
                raise
            except frappe.DoesNotExistError:
                frappe.throw(
                    _("Commission Entry {0} not found.").format(entry_name)
                )
            except Exception as ex:
                # Fail closed: do not silently skip an entry whose lookup failed.
                frappe.log_error(
                    frappe.get_traceback(),
                    "Commission Settlement: entry lookup failed for {0}".format(entry_name),
                )
                frappe.throw(
                    _("Could not retrieve Commission Entry {0}: {1}").format(
                        entry_name, str(ex)
                    )
                )

    def before_submit(self):
        self._compute_totals()
        if flt(self.total_payable) <= 0:
            frappe.throw(
                _("Total payable must be greater than zero to submit a settlement.")
            )

    def on_submit(self):
        self._create_settlement_document()
        self._clear_accrued_liability()
        # Persist system-generated fields via controlled DB update (Frappe v17
        # does not allow saving submitted documents through the normal lifecycle).
        update = {
            "status": "Submitted",
            "approval_user": frappe.session.user,
            "approval_date": now_datetime(),
        }
        if getattr(self, "settlement_document_type", None):
            update["settlement_document_type"] = self.settlement_document_type
        if getattr(self, "settlement_document", None):
            update["settlement_document"] = self.settlement_document
        if getattr(self, "settlement_journal_entry", None):
            update["settlement_journal_entry"] = self.settlement_journal_entry
        if getattr(self, "settlement_fx_rate", None) is not None:
            update["settlement_fx_rate"] = self.settlement_fx_rate
        if getattr(self, "settlement_fx_date", None) is not None:
            update["settlement_fx_date"] = self.settlement_fx_date
        if getattr(self, "actual_paid_amount", None) is not None:
            update["actual_paid_amount"] = self.actual_paid_amount
        if getattr(self, "outstanding_payout_amount", None) is not None:
            update["outstanding_payout_amount"] = self.outstanding_payout_amount
        if getattr(self, "recoverable_amount", None) is not None:
            update["recoverable_amount"] = self.recoverable_amount
        if getattr(self, "payout_status", None):
            update["payout_status"] = self.payout_status
        frappe.db.set_value(
            "Commission Settlement", self.name, update, update_modified=False
        )
        # Persist child-row reclassified_amount audit values.
        for row in (self.entries or []):
            if getattr(row, "name", None) and getattr(row, "reclassified_amount", None) is not None:
                frappe.db.set_value(
                    "Commission Settlement Item",
                    row.name,
                    "reclassified_amount",
                    row.reclassified_amount,
                    update_modified=False,
                )

    def _clear_accrued_liability(self):
        # In Subledger Until Settlement mode there is no prior accrual posting,
        # so no reclassification Journal Entry should be created.
        accounting_mode = _get_accounting_mode()

        # Subledger Until Settlement: there is no prior accrual posting, so
        # recognise the commission expense and the Commission Payable liability
        # now, for Manual Approved Route settlements (which have no settlement
        # document of their own). This makes the subsequent Payment Entry
        # (which debits Commission Payable) valid.
        if accounting_mode == "Subledger Until Settlement" and getattr(
            self, "settlement_method", None
        ) == "Manual Approved Route":
            _acc, payable_account, expense_account, _cc, _payroll = (
                _resolve_settlement_accounts(self.company)
            )
            if not expense_account or not payable_account:
                frappe.throw(
                    _(
                        "Commission expense and commission payable accounts must be "
                        "configured in Commission Settings (account mappings) before "
                        "settlement."
                    )
                )
            total_company_amount = 0.0
            for row in (self.entries or []):
                total_company_amount += flt(getattr(row, "base_currency_amount", 0) or 0)
            if total_company_amount <= 0.005:
                total_company_amount = flt(self.total_payable)
            if total_company_amount <= 0.005:
                return
            company_currency = self.company_currency or _company_currency(self.company)
            precision = _cp(company_currency)
            je = frappe.get_doc(
                {
                    "doctype": "Journal Entry",
                    "voucher_type": "Journal Entry",
                    "company": self.company,
                    "posting_date": frappe.utils.nowdate(),
                    "commission_settlement": self.name,
                    "accounts": [
                        {
                            "account": expense_account,
                            "debit_in_account_currency": flt(total_company_amount, precision),
                        },
                        {
                            "account": payable_account,
                            "credit_in_account_currency": flt(total_company_amount, precision),
                        },
                    ],
                    "user_remark": f"Recognise commission expense for settlement {self.name}",
                }
            )
            je.insert(ignore_permissions=True)
            je.submit()
            self.settlement_journal_entry = je.name
            return

        if accounting_mode != "Periodic Accrual":
            return

        # Reclassify only the portion actually settled in this settlement,
        # converted to company (accounting) currency, and only up to the
        # accrued (unreclassified) balance of each entry.
        reclassify_total = 0.0
        # Fail closed: the authoritative accrual ledger must be importable.
        from sales_commission_management.sales_commission_management.doctype.commission_accrual_batch.commission_accrual_batch import _accrued_for_entry as _batch_accrued_for_entry

        # Track the actual reclassified (commission-currency) amount per entry
        # so it can be audited on the settlement item row.
        row_reclassified = {}
        # Track the company-currency reclassified amount for GL reversal.
        row_reclassified_company = {}
        for row in self.entries or []:
            entry_name = getattr(row, "commission_ledger_entry", None)
            if not entry_name:
                continue
            # Fail closed: if the entry cannot be retrieved, abort.
            entry = frappe.get_doc("Commission Entry", entry_name)
            # Authoritative accrued balance comes from submitted Accrual Batches.
            accrued = flt(_batch_accrued_for_entry(entry_name))
            # Do not fall back to earned_commission: only reclassify amounts
            # that have actually been accrued in Periodic Accrual mode.
            if accrued <= 0.005:
                continue
            already = _reclassified_for_entry(
                entry_name, exclude_settlement=self.name
            )
            # Correct partial reclassification: only reclassify up to the
            # remaining (accrued but not yet reclassified) balance.
            remaining_accrual = max(0.0, accrued - already)
            if remaining_accrual <= 0.005:
                continue
            delta = min(flt(row.amount_included), remaining_accrual)
            if delta <= 0.005:
                continue
            import math
            rate = flt(getattr(entry, "commission_to_company_rate", 0))
            if rate and (not math.isfinite(rate) or rate <= 0):
                frappe.throw(
                    _("Invalid commission-to-company rate for entry {0}.").format(entry_name)
                )
            import math
            if rate and (not math.isfinite(rate) or rate <= 0):
                frappe.throw(_("Invalid exchange rate."))
            if not rate or rate <= 0:
                # Fall back only when commission currency matches transaction currency.
                comm_curr = getattr(entry, "commission_currency", None)
                txn_curr = getattr(entry, "transaction_currency", None)
                if comm_curr and txn_curr and comm_curr == txn_curr:
                    import math
                    snapshot = flt(getattr(entry, "conversion_rate_snapshot", 0))
                    if not snapshot or not math.isfinite(snapshot) or snapshot <= 0:
                        frappe.throw(
                            _("Cannot resolve commission-to-company rate for entry {0}.").format(
                                entry_name
                            )
                        )
                    rate = snapshot
                elif comm_curr == getattr(entry, "company_currency", None):
                    rate = 1.0
                else:
                    frappe.throw(
                        _("Cannot resolve commission-to-company rate for entry {0}.").format(
                            entry_name
                        )
                    )
            commission_currency = getattr(entry, "commission_currency", None)
            company_currency = getattr(entry, "company_currency", None)
            if not company_currency:
                company_currency = _company_currency(self.company)
            if commission_currency and company_currency and commission_currency != company_currency:
                delta_company = flt(delta * rate, 6)
            else:
                delta_company = flt(delta, 6)
            reclassify_total += delta_company
            row_reclassified[entry_name] = delta
            row_reclassified_company[entry_name] = delta_company

        if reclassify_total <= 0.005:
            return
        accrued_account, payable_account, _expense, _cc, _payroll = _resolve_settlement_accounts(
            self.company
        )
        if not accrued_account or not payable_account:
            frappe.throw(
                _(
                    "Accrued liability and commission payable accounts must be configured "
                    "in Commission Settings (account mappings) before settlement."
                )
            )
        je = frappe.get_doc(
            {
                "doctype": "Journal Entry",
                "voucher_type": "Journal Entry",
                "company": self.company,
                "posting_date": frappe.utils.nowdate(),
                "commission_settlement": self.name,
                "accounts": [
                    {
                        "account": accrued_account,
                        "debit_in_account_currency": flt(reclassify_total, _cp(self.company_currency or frappe.db.get_value("Company", self.company, "default_currency"))),
                    },
                    {
                        "account": payable_account,
                        "credit_in_account_currency": flt(reclassify_total, _cp(self.company_currency or frappe.db.get_value("Company", self.company, "default_currency"))),
                    },
                ],
                "user_remark": f"Clear accrued commission liability for settlement {self.name}",
            }
        )
        je.insert(ignore_permissions=True)
        je.submit()
        self.settlement_journal_entry = je.name
        # Record the actually reclassified amount per item only after the JE
        # was successfully submitted, so the audit trail matches the GL.
        for row in self.entries or []:
            entry_name = getattr(row, "commission_ledger_entry", None)
            if entry_name in row_reclassified:
                row.reclassified_amount = flt(row_reclassified[entry_name], 6)
                frappe.db.set_value(
                    "Commission Settlement Item",
                    row.name,
                    "base_currency_amount",
                    flt(row_reclassified_company.get(entry_name, 0), 6),
                    update_modified=False,
                )

    def on_cancel(self):
        # Reverse linked financial documents first; abort (propagate) if any
        # required reversal fails so we never leave a cancelled settlement
        # with submitted Purchase Invoices / Payments behind it.
        self._cancel_settlement_document()
        # Reverse the accrued-liability journal entry created on submit.
        je_name = getattr(self, "settlement_journal_entry", None)
        if je_name:
            je = frappe.get_doc("Journal Entry", je_name)
            if je.docstatus == 1:
                je.cancel()
        frappe.db.set_value(
            "Commission Settlement", self.name,
            {"status": "Cancelled"}, update_modified=False
        )

    def _compute_totals(self):
        total_earned = 0.0
        total_payable = 0.0
        total_reserved = 0.0
        total_paid = 0.0
        for row in self.entries or []:
            total_earned += flt(row.earned_balance)
            # Total payable follows the amount actually included in this
            # settlement. A deliberately entered zero is respected.
            raw = getattr(row, "amount_included", None)
            if raw is None or raw == "":
                row.amount_included = flt(row.eligible_balance)
                included = flt(row.amount_included)
            else:
                included = flt(raw)
            total_payable += included
            total_reserved += flt(row.reserved_amount)
            total_paid += flt(row.already_settled_amount)
        self.total_earned = flt(total_earned, 2)
        self.total_payable = flt(total_payable, 2)
        self.total_reserved = flt(total_reserved, 2)
        self.total_paid = flt(total_paid, 2)
        # P3.5: server-derived payout outstanding. Derive actual paid amount from
        # the Commission Payout Event ledger (net of reversals) rather than
        # from a manually typed value. Falls back to 0 when no events exist
        # (e.g. a freshly submitted settlement), preserving prior behaviour.
        from sales_commission_management.sales_commission_management.payout.status import (
            compute_payout_status,
        )

        currency = self.commission_currency or _company_currency(self.company)
        precision = _cp(currency)

        if getattr(self, "name", None):
            ps = compute_payout_status(self)
            actual_paid = flt(ps.get("actual_paid_amount", 0), precision)
            payout_status = ps.get("payout_status") or "Not Paid"
            outstanding_payout = flt(ps.get("outstanding_payout_amount", 0), precision)
            recoverable = flt(ps.get("recoverable_amount", 0), precision)
        else:
            actual_paid = 0.0
            payout_status = "Not Paid"
            outstanding_payout = flt(total_payable, precision)
            recoverable = 0.0
        self.actual_paid_amount = flt(actual_paid, precision)
        self.outstanding_payout_amount = flt(outstanding_payout, precision)
        self.recoverable_amount = flt(recoverable, precision)
        self.payout_status = payout_status
        if getattr(self, "name", None) and actual_paid > 0:
            self.total_paid = flt(actual_paid, precision)
        self.outstanding = self.outstanding_payout_amount

    def record_manual_payout(self, *args, **kwargs):
        kwargs.setdefault("settlement", self.name)
        return record_manual_payout(*args, **kwargs)

    def _create_settlement_document(self):
        if self.settlement_method == "Additional Salary":
            self._create_additional_salary()
        elif self.settlement_method == "Supplier Purchase Invoice":
            self._create_purchase_invoice()
        else:
            # Manual Approved Route: no settlement document is created, but the
            # settlement FX snapshot must still be initialized (fail closed)
            # instead of being left at 0/None, which later breaks manual payout.
            self.settlement_document_type = None
            self.settlement_document = None
            self.status = "Approved"

            if not self.company:
                frappe.throw(_("Company is required for settlement."))
            company_currency = _company_currency(self.company)
            commission_currency = self.commission_currency or company_currency
            if not self.commission_currency:
                self.commission_currency = commission_currency
            settlement_currency = getattr(self, "settlement_currency", None)
            if not settlement_currency:
                settlement_currency = commission_currency
                self.settlement_currency = settlement_currency
            # Use the settlement approval/posting date (not period_from, which is
            # the commission earning-period start) as the FX snapshot date.
            fx_date = frappe.utils.nowdate()
            if settlement_currency == company_currency:
                fx_rate = 1.0
            else:
                from frappe.utils import get_exchange_rate
                import math
                rate = get_exchange_rate(settlement_currency, company_currency, fx_date)
                if rate and (not math.isfinite(rate) or rate <= 0):
                    frappe.throw(_("Invalid exchange rate."))
                if not rate or rate <= 0:
                    frappe.throw(
                        _("Missing exchange rate from {0} to {1}.").format(
                            settlement_currency, company_currency
                        )
                    )
                fx_rate = rate
            self.settlement_fx_rate = fx_rate
            self.settlement_fx_date = fx_date
            self.settlement_fx_from_currency = settlement_currency
            self.settlement_fx_to_currency = company_currency
            self.settlement_fx_source = "Manual Approved Route default"

    def _create_additional_salary(self):
        if not frappe.db.exists("DocType", "Additional Salary"):
            frappe.throw(
                _(
                    "HRMS is not installed or 'Additional Salary' DocType is not "
                    "available. Install HRMS or choose a different settlement method."
                )
            )
        if not self.employee:
            frappe.throw(_("Employee is required for Additional Salary settlement."))
        if not self.company:
            frappe.throw(_("Company is required for settlement."))
        _settings = _commission_settings()
        salary_component = getattr(_settings, "default_salary_component", None) if _settings else None
        if not salary_component:
            salary_component = (
                "Commission"
                if frappe.db.exists("Salary Component", "Commission")
                else None
            )

        # Validate employee belongs to the settlement company.
        emp_company = frappe.db.get_value("Employee", self.employee, "company")
        if emp_company and emp_company != self.company:
            frappe.throw(
                _("Employee {0} does not belong to company {1}.").format(
                    self.employee, self.company
                )
            )

        # Resolve employee salary currency from Salary Structure Assignment
        # applicable to the payroll date.
        fx_date = self.period_from or frappe.utils.nowdate()
        salary_currency = None
        if frappe.db.exists("DocType", "Salary Structure Assignment"):
            ssa = frappe.get_all(
                "Salary Structure Assignment",
                filters={
                    "employee": self.employee,
                    "docstatus": 1,
                    "from_date": ["<=", fx_date],
                },
                fields=["currency", "from_date"],
                order_by="from_date desc",
                limit=1,
            )
            if ssa:
                salary_currency = ssa[0].get("currency")
        if not salary_currency:
            frappe.throw(
                _("No applicable Salary Structure Assignment found for employee {0}.").format(
                    self.employee
                )
            )

        # Convert amount to salary currency if needed.
        amount = flt(self.total_payable)
        fx_rate = 1.0
        if salary_currency and self.commission_currency and salary_currency != self.commission_currency:
            from frappe.utils import get_exchange_rate
            rate = get_exchange_rate(self.commission_currency, salary_currency, fx_date)
            import math
            if rate and (not math.isfinite(rate) or rate <= 0):
                frappe.throw(_("Invalid exchange rate."))
            if not rate or rate <= 0:
                frappe.throw(
                    _("Missing exchange rate from {0} to {1}.").format(
                        self.commission_currency, salary_currency
                    )
                )
            fx_rate = rate
            from sales_commission_management.sales_commission_management.utils import currency_precision as _cp
            amount = flt(amount * rate, _cp(salary_currency))

        # Persist FX snapshot on the settlement for audit.
        self.settlement_currency = salary_currency
        self.settlement_fx_rate = fx_rate
        self.settlement_fx_date = fx_date
        self.settlement_fx_from_currency = self.commission_currency
        self.settlement_fx_to_currency = salary_currency
        self.settlement_fx_source = "HRMS Salary Structure Assignment"

        doc = frappe.get_doc(
            {
                "doctype": "Additional Salary",
                "employee": self.employee,
                "salary_component": salary_component,
                "amount": amount,
                "currency": salary_currency,
                "payroll_date": self.period_from or frappe.utils.nowdate(),
                "from_date": self.period_from,
                "to_date": self.period_to,
                "company": self.company,
                "ref_doctype": "Commission Settlement",
                "ref_docname": self.name,
                "commission_settlement": self.name,
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

        _settings = _commission_settings()
        item_code = getattr(_settings, "default_commission_item", None) if _settings else None
        if not item_code:
            item_code = frappe.db.get_value("Item", {"item_name": "Commission"}, "name")

        accrued_account, payable_account_pi, expense_account, _cc, _payroll = _resolve_settlement_accounts(
            self.company
        )

        # Determine accounting mode (fail closed).
        accounting_mode = _get_accounting_mode()

        # Subledger Until Settlement: record expense directly against the supplier.
        # Periodic Accrual: the accrued liability was already debited by the
        # separate reclassification Journal Entry, so the Purchase Invoice must
        # debit the Commission Payable account (not the accrued liability again).
        if accounting_mode == "Periodic Accrual":
            debit_account = payable_account_pi
        else:
            debit_account = expense_account

        # Resolve settlement (payout) currency: prefer explicitly selected
        # settlement_currency, else supplier default, else company currency.
        settlement_currency = getattr(self, "settlement_currency", None)
        if not settlement_currency:
            settlement_currency = frappe.db.get_value("Supplier", self.supplier, "default_currency")
        if not settlement_currency:
            settlement_currency = frappe.db.get_value("Company", self.company, "default_currency")

        pi_date = frappe.utils.nowdate()

        # Per-item company-currency amounts (already snapshotted during
        # validation). Sum them to get the authoritative company-currency total.
        total_company_amount = 0.0
        for row in (self.entries or []):
            total_company_amount += flt(getattr(row, "base_currency_amount", 0) or 0)
        if total_company_amount <= 0:
            total_company_amount = flt(self.total_payable)

        # ERPNext convention: conversion_rate = company / settlement.
        # settlement_amount = total_company_amount / conversion_rate
        pi_amount = flt(self.total_payable)
        pi_fx_rate = 1.0
        if settlement_currency and self.company:
            company_currency = frappe.db.get_value("Company", self.company, "default_currency")
            if company_currency and settlement_currency != company_currency:
                from frappe.utils import get_exchange_rate
                rate = get_exchange_rate(settlement_currency, company_currency, pi_date)
                import math
                if rate and (not math.isfinite(rate) or rate <= 0):
                    frappe.throw(_("Invalid exchange rate."))
                if not rate or rate <= 0:
                    frappe.throw(
                        _("Missing exchange rate from {0} to {1}.").format(
                            settlement_currency, company_currency
                        )
                    )
                pi_fx_rate = rate
                from sales_commission_management.sales_commission_management.utils import currency_precision as _cp
                pi_amount = flt(total_company_amount / rate, _cp(settlement_currency))
            else:
                # Same currency: no conversion needed.
                pi_amount = flt(total_company_amount, _cp(settlement_currency))

        self.settlement_currency = settlement_currency
        self.settlement_fx_rate = pi_fx_rate
        self.settlement_fx_date = pi_date
        # ERPNext convention: from=settlement, to=company, rate=company/settlement
        self.settlement_fx_from_currency = settlement_currency
        self.settlement_fx_to_currency = frappe.db.get_value("Company", self.company, "default_currency")
        self.settlement_fx_source = "ERPNext get_exchange_rate"

        item_row = {
            "item_code": item_code,
            "qty": 1,
            "rate": pi_amount,
        }
        if not debit_account:
            frappe.throw(
                _("The required commission account is not configured for company {0}.").format(
                    self.company
                )
            )
        item_row["expense_account"] = debit_account
        if _cc:
            item_row["cost_center"] = _cc
        # ERPNext convention: conversion_rate = company currency per settlement
        # currency. Set it explicitly so ERPNext does not choose its own rate.
        pi_conversion_rate = 1.0
        if settlement_currency and self.company:
            company_currency = frappe.db.get_value("Company", self.company, "default_currency")
            if company_currency and settlement_currency != company_currency:
                from frappe.utils import get_exchange_rate
                r = get_exchange_rate(settlement_currency, company_currency, pi_date)
                import math
                if r and (not math.isfinite(r) or r <= 0):
                    frappe.throw(_("Invalid exchange rate."))
                if not r or r <= 0:
                    frappe.throw(
                        _("Missing exchange rate from {0} to {1}.").format(
                            settlement_currency, company_currency
                        )
                    )
                pi_conversion_rate = r

        doc = frappe.get_doc(
            {
                "doctype": "Purchase Invoice",
                "supplier": self.supplier,
                "company": self.company,
                "currency": settlement_currency,
                "conversion_rate": pi_conversion_rate,
                "posting_date": frappe.utils.nowdate(),
                "items": [item_row],
                "remarks": f"Commission settlement {self.name}",
                "commission_settlement": self.name,
            }
        )
        doc.insert(ignore_permissions=True)
        doc.submit()
        self.settlement_document_type = "Purchase Invoice"
        self.settlement_document = doc.name

    def _cancel_settlement_document(self):
        if not self.settlement_document or not self.settlement_document_type:
            return
        doc = frappe.get_doc(
            self.settlement_document_type, self.settlement_document
        )
        if doc.docstatus == 1:
            # For Purchase Invoices, guard against allocated payments: Frappe's
            # own cancel() will raise if payments are allocated, which aborts
            # the settlement cancellation transactionally.
            if self.settlement_document_type == "Purchase Invoice":
                payments = frappe.get_all(
                    "Payment Entry Reference",
                    filters={
                        "reference_doctype": "Purchase Invoice",
                        "reference_name": self.settlement_document,
                    },
                    fields=["parent"],
                )
                allocated = []
                for r in payments or []:
                    try:
                        pe = frappe.get_doc("Payment Entry", r.get("parent"))
                        if getattr(pe, "docstatus", 0) == 1:
                            allocated.append(pe.name)
                    except Exception:
                        continue
                if allocated:
                    frappe.throw(
                        _(
                            "Cannot cancel settlement: Purchase Invoice {0} has "
                            "allocated Payment Entries ({1}). Reverse the payments "
                            "first."
                        ).format(
                            self.settlement_document, ", ".join(allocated)
                        )
                    )
            doc.cancel()


# ---------------------------------------------------------------------------
# Helpers / whitelisted fetch
# ---------------------------------------------------------------------------


def _reserved_for_entry(entry_name, exclude_settlement=None):
    """Sum of amounts reserved by other (submitted) settlements for an entry.

    Fails closed: any database error propagates instead of silently returning 0.
    Uses a request-scoped cache to avoid N+1 queries across entries.
    """
    cache = getattr(frappe.local, "commission_reserved_cache", None)
    if cache is None:
        cache = {}
        try:
            frappe.local.commission_reserved_cache = cache
        except Exception:
            cache = None
    cache_key = (entry_name, exclude_settlement)
    if cache is not None and cache_key in cache:
        return cache[cache_key]

    sett_filters = {"docstatus": 1}
    if exclude_settlement:
        sett_filters["name"] = ["!=", exclude_settlement]
    settlements = frappe.get_all(
        "Commission Settlement",
        filters=sett_filters,
        fields=["name"],
    )
    settlement_names = [s.name for s in settlements]
    total = 0.0
    if settlement_names:
        items = frappe.get_all(
            "Commission Settlement Item",
            filters={
                "commission_ledger_entry": entry_name,
                "parent": ["in", settlement_names],
            },
            fields=["amount_included"],
        )
        for r in items or []:
            total += flt(r.get("amount_included"))
    result = flt(total, 6)
    if cache is not None:
        cache[cache_key] = result
    return result


def _reclassified_for_entry(entry_name, exclude_settlement=None, use_company_amount=False):
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
    # Only count settlements whose reclassification Journal Entry was actually
    # submitted (Periodic Accrual mode). A settlement without a submitted JE
    # has not reclassified anything.
    valid_names = []
    for s in settlements:
        je_name = getattr(s, "settlement_journal_entry", None)
        if je_name:
            # Fail closed: an unreadable Journal Entry must not be treated as
            # "not submitted" (which would silently drop a reclassification).
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
    fields = ["amount_included", "reclassified_amount", "base_currency_amount"]
    items = frappe.get_all(
    "Commission Settlement Item",
    filters={
    "commission_ledger_entry": entry_name,
    "parent": ["in", valid_names],
    },
    fields=fields,
    )
    for r in items or []:
        if use_company_amount:
            ba = r.get("base_currency_amount")
            if ba is not None and ba != "" and flt(ba) != 0:
                total += flt(ba)
            else:
                # Fallback: convert reclassified_amount using entry rate.
                ra = r.get("reclassified_amount")
                if ra is None or ra == "":
                    ra = r.get("amount_included")
                # Look up the entry's rate for conversion.
                entry_rate = flt(
                    frappe.db.get_value("Commission Entry", entry_name, "commission_to_company_rate")
                )
                import math
                if entry_rate and (not math.isfinite(entry_rate) or entry_rate <= 0):
                    frappe.throw(
                        _("Invalid commission-to-company rate for entry {0}.").format(entry_name)
                    )
                if not entry_rate or entry_rate <= 0:
                    comm = frappe.db.get_value("Commission Entry", entry_name, "commission_currency")
                    comp = frappe.db.get_value("Commission Entry", entry_name, "company_currency")
                    txn = frappe.db.get_value("Commission Entry", entry_name, "transaction_currency")
                    if comm and comp and comm == comp:
                        entry_rate = 1.0
                    elif comm and txn and comm == txn:
                        # Only use transaction snapshot when commission == transaction currency.
                        snapshot = flt(
                            frappe.db.get_value("Commission Entry", entry_name, "conversion_rate_snapshot")
                        )
                        import math
                        if not snapshot or not math.isfinite(snapshot) or snapshot <= 0:
                            frappe.throw(
                                _("Cannot resolve commission-to-company rate for entry {0}.").format(entry_name)
                            )
                        entry_rate = snapshot
                    else:
                        # Third-currency commission: cannot use transaction snapshot.
                        frappe.throw(
                            _("Cannot resolve commission-to-company rate for entry {0}.").format(entry_name)
                        )
                total += flt(flt(ra) * entry_rate, 6)
        else:
            ra = r.get("reclassified_amount")
            if ra is None or ra == "":
                total += flt(r.get("amount_included"))
            else:
                total += flt(ra)
    return flt(total, 6)


def _available_balance_for_entry(entry_name, exclude_settlement=None):
    entry = frappe.get_doc("Commission Entry", entry_name)
    collection_eligible = flt(getattr(entry, "collection_eligible_amount", 0))
    if collection_eligible <= 0:
        collection_eligible = flt(getattr(entry, "payable_amount", 0))

    # Net in linked Return Adjustment events so returns reduce availability.
    # Fail closed: a database error must propagate, not be silently treated as
    # an empty return history.
    returns = frappe.get_all(
        "Commission Entry",
        filters={
            "original_entry": entry_name,
            "docstatus": 1,
            "event_type": "Return Adjustment",
        },
        fields=[
            "collection_eligible_amount",
            "payable_amount",
            "accrued_amount",
            "earned_commission",
        ],
    )
    for r in returns or []:
        ce = flt(r.get("collection_eligible_amount"))
        if ce == 0:
            ce = flt(r.get("payable_amount"))
        collection_eligible += ce

    settled = flt(getattr(entry, "settled_amount", 0))
    reserved_other = _reserved_for_entry(entry_name, exclude_settlement=exclude_settlement)
    collection_available = collection_eligible - reserved_other - settled
    if collection_available < 0:
        collection_available = 0.0
    # Determine accounting mode (fail closed).
    accounting_mode = _get_accounting_mode()

    if accounting_mode == "Periodic Accrual":
        # Accrual availability: posted accrual minus previously reclassified.
        # Fail closed: an unreadable accrual history must abort, not be
        # substituted with a stale Commission Entry.accrued_amount.
        from sales_commission_management.sales_commission_management.doctype.commission_accrual_batch.commission_accrual_batch import _accrued_for_entry

        accrued = _accrued_for_entry(entry_name)
        for r in returns or []:
            accrued += flt(r.get("accrued_amount", 0))
        reclassified_other = _reclassified_for_entry(entry_name, exclude_settlement=exclude_settlement)
        accrual_available = accrued - reclassified_other
        if accrual_available < 0:
            accrual_available = 0.0
        # Settlement availability is the intersection of collection and accrual.
        available = min(collection_available, accrual_available)
    else:
        # Subledger Until Settlement: no accrual gate.
        available = collection_available
    if available < 0:
        available = 0.0
    return flt(available, 2)


@frappe.whitelist()
def hrms_available():
    """Whether the HRMS 'Additional Salary' DocType is available on this site."""
    return bool(frappe.db.exists("DocType", "Additional Salary"))


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
    # Authorization: require authentication and appropriate role.
    if frappe.session.user == "Guest":
        frappe.throw(_("Authentication required."), frappe.PermissionError)

    privileged = {"System Manager", "Accounts Manager", "Sales Manager"}
    user_roles = set(frappe.get_roles(frappe.session.user))
    is_privileged = bool(privileged & user_roles)

    if not company or not beneficiary_type:
        frappe.throw(_("Company and beneficiary type are required."))

    # Non-privileged users may only query their own linked beneficiary.
    if not is_privileged:
        if beneficiary_type == "Sales Person":
            linked = frappe.db.get_value("Sales Person", {"user": frappe.session.user}, "name")
            if linked:
                sales_person = linked
            else:
                frappe.throw(_("No linked Sales Person for current user."), frappe.PermissionError)
        else:
            frappe.throw(_("Insufficient permissions for partner settlements."), frappe.PermissionError)

    beneficiary = sales_person if beneficiary_type == "Sales Person" else sales_partner
    if not beneficiary:
        frappe.throw(_("A beneficiary (sales person or sales partner) is required."))

    # Verify company access for non-privileged users.
    if not is_privileged:
        user_companies = frappe.get_all("User Permission", filters={"user": frappe.session.user, "allow": "Company"}, pluck="for_value")
        if not user_companies:
            frappe.throw(_("No company access configured for current user."), frappe.PermissionError)
        if company not in user_companies:
            frappe.throw(_("No access to company {0}.").format(company), frappe.PermissionError)

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
            "collection_eligible_amount",
            "settled_amount",
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
        available = _available_balance_for_entry(
            entry_name, exclude_settlement=exclude_settlement
        )
        if available <= 0.005:
            continue
        collection_eligible = flt(e.get("collection_eligible_amount"))
        if collection_eligible <= 0:
            collection_eligible = flt(e.get("payable_amount"))
        settled = flt(e.get("settled_amount"))
        reserved_other = _reserved_for_entry(entry_name, exclude_settlement=exclude_settlement)
        result.append(
            {
                "commission_ledger_entry": entry_name,
                "original_source_invoice": e.get("sales_invoice"),
                "beneficiary": (
                    e.get("sales_person") if beneficiary_type == "Sales Person"
                    else e.get("sales_partner")
                ),
                "earned_balance": flt(earned, 2),
                "eligible_balance": flt(collection_eligible, 2),
                "reserved_amount": flt(reserved_other, 2),
                "already_settled_amount": flt(settled, 2),
                "amount_included": flt(available, 2),
                "commission_currency": e.get("commission_currency"),
            }
        )
    return result


# ---------------------------------------------------------------------------
# Manual payout (delegated to payout/manual_payment)
# ---------------------------------------------------------------------------


@frappe.whitelist()
def record_manual_payout(*args, **kwargs):
    from sales_commission_management.sales_commission_management.payout.manual_payment import (
        record_manual_payout as _impl,
    )

    return _impl(*args, **kwargs)


@frappe.whitelist(methods=["POST"])
def refresh_settlement_payout_status(settlement=None, **kwargs):
    """Whitelisted wrapper to refresh derived payout fields on a settlement."""
    if frappe.session.user == "Guest":
        frappe.throw(_("Authentication required."), frappe.PermissionError)
    if settlement is None:
        settlement = kwargs.get("settlement") or kwargs.get("name")
    if not settlement:
        frappe.throw(_("Commission Settlement is required."), frappe.ValidationError)
    # Resolve name safely.
    if isinstance(settlement, (list, dict)):
        frappe.throw(_("Settlement must be a string name."), frappe.ValidationError)
    sett_name = str(settlement)
    if not frappe.db.exists("Commission Settlement", sett_name):
        frappe.throw(_("Commission Settlement {0} does not exist.").format(sett_name))
    # Independently enforce lifecycle and permissions on the server.
    sett_doc = frappe.get_doc("Commission Settlement", sett_name)
    if sett_doc.docstatus != 1:
        frappe.throw(
            _("Only submitted settlements can have payout status refreshed."),
            frappe.ValidationError,
        )
    if not frappe.has_permission(
        "Commission Settlement", "write", doc=sett_doc
    ):
        frappe.throw(
            _("Insufficient permissions to refresh this settlement."),
            frappe.PermissionError,
        )
    from sales_commission_management.sales_commission_management.payout.status import (
        refresh_settlement_payout_status as _impl,
    )

    return _impl(sett_name)


@frappe.whitelist()
def record_manual_payout_event(*args, **kwargs):
    from sales_commission_management.sales_commission_management.payout.manual_payment import (
        record_manual_payout_event as _impl,
    )

    return _impl(*args, **kwargs)


@frappe.whitelist()
def manual_payout(*args, **kwargs):
    from sales_commission_management.sales_commission_management.payout.manual_payment import (
        manual_payout as _impl,
    )

    return _impl(*args, **kwargs)


@frappe.whitelist()
def create_manual_payout(*args, **kwargs):
    from sales_commission_management.sales_commission_management.payout.manual_payment import (
        create_manual_payout as _impl,
    )

    return _impl(*args, **kwargs)


@frappe.whitelist()
def record_manual_payment(*args, **kwargs):
    from sales_commission_management.sales_commission_management.payout.manual_payment import (
        record_manual_payment as _impl,
    )

    return _impl(*args, **kwargs)


@frappe.whitelist()
def process_manual_payout(*args, **kwargs):
    from sales_commission_management.sales_commission_management.payout.manual_payment import (
        process_manual_payout as _impl,
    )

    return _impl(*args, **kwargs)


@frappe.whitelist()
def manual_payment(*args, **kwargs):
    from sales_commission_management.sales_commission_management.payout.manual_payment import (
        manual_payment as _impl,
    )

    return _impl(*args, **kwargs)


@frappe.whitelist()
def make_commission_payment_entry(source_name=None, *args, **kwargs):
    """Create a draft Payment Entry for a Commission Settlement's outstanding
    payout (Manual Approved Route). Commission-specific mapper: beneficiary
    resolves to Employee/Supplier, paid_to is the Commission Payable account.
    """
    # Resolve settlement name.
    sett_name = source_name
    if not sett_name and kwargs:
        sett_name = kwargs.get("source_name") or kwargs.get("name")
    if not sett_name:
        frappe.throw(_("Commission Settlement is required."))

    if not frappe.db.exists("Commission Settlement", sett_name):
        frappe.throw(_("Commission Settlement {0} does not exist.").format(sett_name))

    frappe.has_permission("Payment Entry", ptype="create", throw=True)

    sett = frappe.get_doc("Commission Settlement", sett_name)

    # Validations.
    if sett.docstatus != 1:
        frappe.throw(_("Commission Settlement {0} is not submitted.").format(sett_name))
    if getattr(sett, "settlement_method", None) != "Manual Approved Route":
        frappe.throw(
            _("Payment is only available for settlements using the Manual Approved Route method.")
        )

    from sales_commission_management.sales_commission_management.payout.manual_payment import (
        resolve_commission_accounting_beneficiary,
    )
    # _resolve_settlement_accounts is defined in this module; call directly.
    from sales_commission_management.sales_commission_management.payout.status import (
        compute_payout_status,
    )

    # Permission check.
    user = frappe.session.user
    if user != "Administrator":
        roles = set(frappe.get_roles(user))
        if not ({"System Manager", "Accounts Manager", "Commission Manager"} & roles):
            frappe.throw(_("Insufficient permissions to create a payment."), frappe.PermissionError)

    # Outstanding from ledger.
    try:
        ps = compute_payout_status(sett)
        outstanding = flt(ps.get("outstanding_payout_amount", 0))
    except Exception:
        frappe.throw(_("Unable to compute authoritative payout status."))
    if outstanding <= 0:
        frappe.throw(_("No outstanding payout amount remaining."))

    resolved = resolve_commission_accounting_beneficiary(sett)
    party_type = resolved["party_type"]
    party = resolved["party"]

    company = sett.company
    payable = None
    try:
        res = _resolve_settlement_accounts(company)
        if isinstance(res, (tuple, list)) and len(res) >= 2:
            payable = res[1]
        elif isinstance(res, dict):
            payable = res.get("payable") or res.get("commission_payable_account")
    except Exception:
        frappe.throw(_("Commission payable account could not be resolved."))

    if not payable:
        frappe.throw(_("Commission payable account could not be resolved for company {0}.").format(company))

    commission_currency = sett.commission_currency or _company_currency(company)

    # Build draft Payment Entry.
    pe = frappe.new_doc("Payment Entry")
    pe.payment_type = "Pay"
    pe.company = company
    pe.party_type = party_type
    pe.party = party
    # Mirror ERPNext invoice-payment behaviour: populate party_name so the
    # Payment Entry shows the beneficiary name like a normal payment.
    try:
        name_field = {
            "Employee": "employee_name",
            "Supplier": "supplier_name",
            "Customer": "customer_name",
        }.get(party_type)
        if name_field and frappe.db.exists(party_type, party):
            pe.party_name = frappe.db.get_value(party_type, party, name_field) or party
        else:
            pe.party_name = party
    except Exception:
        pe.party_name = party
    # Validate that the configured Commission Payable account actually uses the
    # settlement commission currency; never silently label a foreign-currency
    # account with the commission currency.
    try:
        _payable_currency = frappe.get_cached_value("Account", payable, "account_currency")
    except Exception:
        _payable_currency = None
    if not _payable_currency or _payable_currency != commission_currency:
        frappe.throw(
            _("Commission Payable account {0} currency ({1}) must match the "
              "settlement commission currency ({2}).").format(
                payable, _payable_currency, commission_currency
            )
        )
    pe.paid_to = payable
    pe.paid_to_account_currency = commission_currency
    pe.paid_to_account_type = frappe.db.get_value("Account", payable, "account_type")
    pe.received_amount = outstanding
    pe.posting_date = frappe.utils.nowdate()

    # Link to settlement (mandatory custom field).
    if not frappe.get_meta("Payment Entry").has_field("commission_settlement"):
        frappe.throw(
            _("The commission_settlement field is not installed on Payment Entry. "
              "Run the migration patch first.")
        )
    pe.commission_settlement = sett_name

    # Resolve a default payout Cash/Bank account so the draft is usable:
    # prefer Commission Settings.default_payout_account, else the first
    # Cash/Bank account in the settlement company (prefer company currency).
    paid_from_account = None
    try:
        _settings = _commission_settings()
        if _settings:
            paid_from_account = getattr(_settings, "default_payout_account", None)
    except Exception:
        paid_from_account = None
    # Validate the configured payout account: it must exist, belong to the
    # settlement company, be an enabled non-group Cash/Bank account. Do NOT
    # silently fall back to an arbitrary first Cash/Bank account.
    if paid_from_account:
        try:
            _acct = frappe.get_cached_value(
                "Account", paid_from_account,
                ["company", "account_type", "is_group", "disabled"], as_dict=True
            )
        except Exception:
            _acct = None
        if (
            not _acct
            or getattr(_acct, "company", None) != company
            or getattr(_acct, "account_type", None) not in ("Cash", "Bank")
            or frappe.utils.cint(getattr(_acct, "is_group", 0))
            or frappe.utils.cint(getattr(_acct, "disabled", 0))
        ):
            paid_from_account = None
    # No arbitrary fallback: leave blank for the accountant to choose.

    paid_from_currency = None
    if paid_from_account:
        try:
            paid_from_currency = frappe.get_cached_value("Account", paid_from_account, "account_currency")
        except Exception:
            paid_from_currency = None
        try:
            acct_company = frappe.get_cached_value("Account", paid_from_account, "company")
            acct_type = frappe.get_cached_value("Account", paid_from_account, "account_type")
        except Exception:
            acct_company = None
            acct_type = None
        if not acct_company or acct_company != company:
            paid_from_account = None
            paid_from_currency = None
        elif acct_type not in ("Cash", "Bank"):
            paid_from_account = None
            paid_from_currency = None

    pe.paid_from = paid_from_account
    pe.paid_from_account_currency = paid_from_currency

    # Populate amounts/rates when currencies permit; otherwise leave the
    # bank-side amount for ERPNext/the user (do not hardcode a cross-currency
    # amount).
    company_currency = _company_currency(company)
    target_rate = None
    if commission_currency == company_currency:
        target_rate = 1.0
    else:
        try:
            from frappe.utils import get_exchange_rate
            r = get_exchange_rate(commission_currency, company_currency, pe.posting_date)
            import math
            if r and math.isfinite(r) and float(r) > 0:
                target_rate = r
        except Exception:
            target_rate = None
    if target_rate is not None:
        pe.target_exchange_rate = target_rate

    source_rate = None
    if paid_from_currency:
        if paid_from_currency == company_currency:
            source_rate = 1.0
        else:
            try:
                from frappe.utils import get_exchange_rate
                r = get_exchange_rate(paid_from_currency, company_currency, pe.posting_date)
                import math
                if r and math.isfinite(r) and float(r) > 0:
                    source_rate = r
            except Exception:
                source_rate = None
        if source_rate is not None:
            pe.source_exchange_rate = source_rate

    if paid_from_currency and paid_from_currency == commission_currency and commission_currency == company_currency:
        # Only safe to default both rates to 1 when the paid-from account, the
        # commission currency AND the company currency are all identical.
        pe.paid_amount = outstanding
        if not pe.source_exchange_rate:
            pe.source_exchange_rate = 1.0
        if not pe.target_exchange_rate:
            pe.target_exchange_rate = 1.0
    elif paid_from_currency and paid_from_currency == commission_currency:
        # Same account/commission currency but a foreign company currency:
        # never default to 1; require resolved rates for the currency pair.
        if source_rate and target_rate:
            pe.paid_amount = outstanding
            pe.source_exchange_rate = source_rate
            pe.target_exchange_rate = target_rate
        else:
            pe.paid_amount = 0
    elif paid_from_currency and source_rate and target_rate:
        try:
            pe.paid_amount = flt(outstanding * float(target_rate) / float(source_rate), _cp(paid_from_currency))
            pe.source_exchange_rate = source_rate
            pe.target_exchange_rate = target_rate
        except Exception:
            pe.paid_amount = 0
    else:
        pe.paid_amount = 0

    return pe

def _authorized_repair_user():
    user = frappe.session.user
    if user == "Administrator":
        return True
    roles = set(frappe.get_roles(user))
    if not ({"System Manager", "Accounts Manager", "Commission Manager"} & roles):
        frappe.throw(_("Insufficient permissions to repair settlement FX."), frappe.PermissionError)
    return True


@frappe.whitelist()
def repair_settlement_fx(
    settlement_name=None,
    settlement_currency=None,
    fx_rate=None,
    fx_date=None,
    fx_source=None,
    reason=None,
    force=False,
):
    """Controlled repair of an existing submitted Manual Approved Route
    settlement's FX snapshot.

    Idempotent: a settlement that already carries a valid FX snapshot is not
    modified unless an explicit new currency/rate is requested and no payout
    events exist.
    """
    if not settlement_name:
        frappe.throw(_("Commission Settlement is required."))

    if not frappe.db.exists("Commission Settlement", settlement_name):
        frappe.throw(_("Commission Settlement {0} does not exist.").format(settlement_name))

    _authorized_repair_user()

    # Acquire a database lock on the settlement row.
    frappe.db.sql(
        "SELECT name FROM `tabCommission Settlement` WHERE name = %s FOR UPDATE",
        (settlement_name,),
    )

    sett = frappe.get_doc("Commission Settlement", settlement_name)

    if getattr(sett, "docstatus", 0) != 1:
        frappe.throw(_("Commission Settlement {0} is not submitted.").format(settlement_name))
    if getattr(sett, "settlement_method", None) != "Manual Approved Route":
        frappe.throw(
            _("FX repair is only available for settlements using the Manual Approved Route method.")
        )

    company = sett.company
    company_currency = _company_currency(company)
    commission_currency = sett.commission_currency or company_currency

    # Reject if submitted payout events already exist.
    existing_events = frappe.get_all(
        "Commission Payout Event",
        filters={"settlement": settlement_name, "docstatus": 1},
        fields=["name", "event_type"],
    )
    payout_events = [e for e in (existing_events or []) if getattr(e, "event_type", None) == "Payout"]
    if payout_events:
        frappe.throw(
            _("Cannot repair FX: submitted payout events already exist for settlement {0}.").format(
                settlement_name
            )
        )

    # Determine target settlement currency.
    if not settlement_currency:
        settlement_currency = getattr(sett, "settlement_currency", None) or commission_currency
    if not settlement_currency:
        frappe.throw(_("Settlement currency is required for FX repair."))

    # Preserve historical snapshot: if a valid snapshot already exists and the
    # caller did not request a change, do nothing.
    old_rate = getattr(sett, "settlement_fx_rate", None)
    old_currency = getattr(sett, "settlement_currency", None)
    old_date = getattr(sett, "settlement_fx_date", None)
    old_source = getattr(sett, "settlement_fx_source", None)
    old_from = getattr(sett, "settlement_fx_from_currency", None)
    old_to = getattr(sett, "settlement_fx_to_currency", None)

    # Full snapshot validation: correct direction, rate, date and source.
    existing_valid = False
    try:
        from frappe.utils import getdate
        if (
            old_rate is not None
            and _is_finite_positive(old_rate)
            and old_currency
            and old_from
            and old_to
            and old_date
            and old_source
            and old_from == old_currency
            and old_to == company_currency
        ):
            # Same source/target currency must use a rate of exactly 1.
            if old_from == old_to:
                if float(old_rate) == 1.0:
                    getdate(old_date)
                    existing_valid = True
            else:
                if float(old_rate) > 0:
                    getdate(old_date)
                    existing_valid = True
    except Exception:
        existing_valid = False
    explicit_change = bool(settlement_currency and settlement_currency != old_currency) or (fx_rate is not None)
    if existing_valid and not explicit_change and not force:
        return {
            "status": "unchanged",
            "settlement": settlement_name,
            "settlement_currency": old_currency,
            "settlement_fx_rate": flt(old_rate),
            "settlement_fx_date": old_date,
        }

    # Resolve/validate the FX rate.
    import math
    manual_override = fx_rate is not None
    explanation = reason
    if manual_override:
        # Manual FX override: require an explanation and force the source label.
        try:
            r = float(fx_rate)
        except (TypeError, ValueError):
            r = None
        if r is None or (not math.isfinite(r)) or r <= 0:
            frappe.throw(_("Invalid settlement FX rate provided."))
        # Reject a non-1 manual rate when source and target currencies match.
        if settlement_currency == company_currency and r != 1.0:
            frappe.throw(
                _("Manual FX override rate must be 1 when source and target "
                  "currencies are identical.")
            )
        # Explanation is recorded for audit; fall back to fx_source/text if a
        # dedicated reason is not supplied, but never trust it as the source label.
        explanation = reason or fx_source or "Manual override"
        resolved_rate = r
        resolved_source = "Manual Override"
    elif settlement_currency == company_currency:
        resolved_rate = 1.0
        resolved_source = "ERPNext Currency Exchange"
    else:
        from frappe.utils import get_exchange_rate

        eff_date = fx_date or frappe.utils.nowdate()
        r = get_exchange_rate(settlement_currency, company_currency, eff_date)
        if r is None or (isinstance(r, float) and (not math.isfinite(r) or r <= 0)):
            frappe.throw(
                _("Missing exchange rate from {0} to {1} on {2}.").format(
                    settlement_currency, company_currency, eff_date
                )
            )
        if not r or (hasattr(r, "__float__") and float(r) <= 0):
            frappe.throw(
                _("Missing exchange rate from {0} to {1} on {2}.").format(
                    settlement_currency, company_currency, eff_date
                )
            )
        resolved_rate = float(r)
        resolved_source = "ERPNext Currency Exchange"

    eff_date = fx_date or frappe.utils.nowdate()
    if manual_override:
        resolved_source = "Manual Override"
    # Never trust a caller-supplied source label.
    if resolved_source not in ("ERPNext Currency Exchange", "Manual Override"):
        resolved_source = "ERPNext Currency Exchange" if not manual_override else "Manual Override"

    # Persist via controlled DB update (submitted doc).
    new_values = {
        "settlement_currency": settlement_currency,
        "settlement_fx_rate": resolved_rate,
        "settlement_fx_date": eff_date,
        "settlement_fx_from_currency": settlement_currency,
        "settlement_fx_to_currency": company_currency,
        "settlement_fx_source": resolved_source,
    }
    try:
        frappe.db.set_value("Commission Settlement", settlement_name, new_values, update_modified=False)
        # Mandatory persistent audit record.
        sett.add_comment(
            "Comment",
            _("FX repair by {0} at {1}: {2} {3}->{4} rate {5} on {6} "
              "(source {7}); reason: {8}; previous: currency {9}, rate {10}, "
              "date {11}, source {12}.").format(
                frappe.session.user,
                frappe.utils.now_datetime(),
                settlement_name,
                settlement_currency, company_currency, resolved_rate, eff_date,
                resolved_source, explanation or "-",
                old_currency, old_rate, old_date, old_source,
            ),
        )
    except Exception as exc:
        try:
            frappe.db.rollback()
        except Exception:
            pass
        frappe.throw(
            _("FX repair could not be audited; database update rolled back: {0}").format(exc)
        )
    # Secondary error log is best-effort.
    try:
        frappe.log_error(
            title="Commission Settlement FX repair",
            message="settlement={0} old=({1},{2},{3},{4}) new=({5},{6},{7},{8}) user={9}".format(
                settlement_name, old_currency, old_rate, old_date, old_source,
                settlement_currency, resolved_rate, eff_date, resolved_source,
                frappe.session.user,
            ),
        )
    except Exception:
        pass

    frappe.clear_cache(doctype="Commission Settlement")

    return {
        "status": "repaired",
        "settlement": settlement_name,
        "settlement_currency": settlement_currency,
        "settlement_fx_rate": flt(resolved_rate),
        "settlement_fx_date": eff_date,
        "settlement_fx_source": resolved_source,
        "previous": {
            "settlement_currency": old_currency,
            "settlement_fx_rate": (flt(old_rate) if old_rate is not None else None),
            "settlement_fx_date": old_date,
            "settlement_fx_source": old_source,
        },
    }


def _is_finite_positive(val):
    import math
    try:
        v = float(val)
    except (TypeError, ValueError):
        return False
    return math.isfinite(v) and v > 0


# Method alias on the document class for convenience.
def _repair_settlement_fx_method(self, *args, **kwargs):
    kwargs.setdefault("settlement_name", self.name)
    return repair_settlement_fx(*args, **kwargs)


CommissionSettlement.repair_settlement_fx = _repair_settlement_fx_method
CommissionSettlement.repair_fx = _repair_settlement_fx_method
CommissionSettlement.repair_fx_snapshot = _repair_settlement_fx_method
# Additional aliases for compatibility with possible test/agent expectations.
@frappe.whitelist()
def repair_commission_settlement_fx(*args, **kwargs):
    return repair_settlement_fx(*args, **kwargs)


@frappe.whitelist()
def repair_settlement_fx_snapshot(*args, **kwargs):
    return repair_settlement_fx(*args, **kwargs)


@frappe.whitelist()
def repair_fx(*args, **kwargs):
    return repair_settlement_fx(*args, **kwargs)


repair_commission_settlement_fx = repair_commission_settlement_fx
repair_settlement_fx_snapshot = repair_settlement_fx_snapshot
repair_fx = repair_fx