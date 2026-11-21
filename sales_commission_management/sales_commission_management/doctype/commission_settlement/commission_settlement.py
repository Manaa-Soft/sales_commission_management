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


def _get_accounting_mode():
    """Return the configured accounting mode. Fail closed if not configured."""
    if not frappe.db.exists("Commission Settings"):
        frappe.throw(
            _("Commission Settings must be configured before settlement.")
        )
    settings = frappe.get_single("Commission Settings")
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
    if not frappe.db.exists("Commission Settings"):
        return accrued, payable, expense, cost_center, payroll_expense
    settings = frappe.get_single("Commission Settings")
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
                if not getattr(row, "source_currency", None) and getattr(row, "original_source_invoice", None):
                    try:
                        row.source_currency = frappe.db.get_value(
                            "Sales Invoice", row.original_source_invoice, "currency"
                        )
                    except Exception:
                        row.source_currency = None
                if not getattr(row, "company_currency", None):
                    row.company_currency = _company_currency(self.company)
                if not row.beneficiary:
                    row.beneficiary = entry.sales_person or entry.sales_partner
                if not row.original_source_invoice:
                    row.original_source_invoice = getattr(entry, "sales_invoice", None)

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
        # amount_included already excludes previously settled amounts, so
        # outstanding for this settlement is the included amount itself.
        self.outstanding = flt(total_payable, 2)

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
        self.settlement_fx_rate = fx_rate
        self.settlement_fx_date = fx_date

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

        # Resolve supplier currency for the Purchase Invoice.
        supplier_currency = frappe.db.get_value("Supplier", self.supplier, "default_currency")
        if not supplier_currency:
            supplier_currency = frappe.db.get_value("Company", self.company, "default_currency")

        pi_date = frappe.utils.nowdate()

        # Convert settlement amount to supplier currency if needed.
        pi_amount = flt(self.total_payable)
        pi_fx_rate = 1.0
        if supplier_currency and self.commission_currency and supplier_currency != self.commission_currency:
            from frappe.utils import get_exchange_rate
            rate = get_exchange_rate(self.commission_currency, supplier_currency, pi_date)
            import math
            if rate and (not math.isfinite(rate) or rate <= 0):
                frappe.throw(_("Invalid exchange rate."))
            if not rate or rate <= 0:
                frappe.throw(
                    _("Missing exchange rate from {0} to {1}.").format(
                        self.commission_currency, supplier_currency
                    )
                )
            pi_fx_rate = rate
            from sales_commission_management.sales_commission_management.utils import currency_precision as _cp
            pi_amount = flt(pi_amount * rate, _cp(supplier_currency))

        # Persist FX snapshot on the settlement for audit.
        self.settlement_fx_rate = pi_fx_rate
        self.settlement_fx_date = pi_date

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
        doc = frappe.get_doc(
            {
                "doctype": "Purchase Invoice",
                "supplier": self.supplier,
                "company": self.company,
                "currency": supplier_currency,
                "posting_date": frappe.utils.nowdate(),
                "items": [item_row],
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
