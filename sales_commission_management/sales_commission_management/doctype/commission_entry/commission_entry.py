import frappe
from frappe import _
from sales_commission_management.sales_commission_management.utils import currency_precision as _cp
from frappe.model.document import Document
from frappe.utils import flt, getdate


class CommissionEntry(Document):
    def validate(self):
        # Submitted entries are immutable except for payment-allocation fields,
        # which are written via frappe.db.set_value (see update_commission_payment_status).
        if self.docstatus == 1 and self.is_new():
            return
        if self.docstatus == 1:
            # Allow only payment-tracking field updates performed by the engine.
            immutable_fields = {
                "earned_commission",
                "commission_basis_amount",
                "collection_events",
                "transaction_basis_amount",
                "company_basis_amount",
                "commission_rate",
                "commission_to_company_rate",
                "fixed_amount_snapshot",
                "commission_currency",
                "transaction_currency",
                "company_currency",
                "conversion_rate_snapshot",
                "exchange_rate",
                "earned_delta",
                "basis_name",
                "item_code",
                "item_group",
                "source_qty",
                "sales_invoice",
                "posting_date",
                "company",
                "sales_person",
                "sales_partner",
                "plan",
                "rule",
                "pool_code",
                "beneficiary_type",
                "event_type",
                "event_key",
                "original_entry",
                "return_invoice",
                "reverses_entry",
                "source_row",
                "original_invoice",
                "source_name",
                "source_doctype",
                "calculation_snapshot",
                "commission_basis",
            }
            for f in immutable_fields:
                if self.has_value_changed(f):
                    frappe.throw(
                        _(
                            "Submitted Commission Entry field '{0}' cannot be modified."
                        ).format(f)
                    )

    def on_submit(self):
        pass

    def on_cancel(self):
        pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _company_currency(company):
    return (
        frappe.get_cached_value("Company", company, "default_currency")
        if company
        else None
    )


def _resolve_commission_currency(plan, doc, company_currency, txn_currency, exchange_rate):
    """Resolve commission currency and its rate to company currency.

    Returns (commission_currency, commission_to_company_rate).
    """
    policy = getattr(plan, "commission_currency_policy", None) or "Company Currency"
    if policy == "Transaction Currency":
        commission_currency = txn_currency or company_currency
    elif policy == "Fixed Currency":
        commission_currency = getattr(plan, "commission_currency", None) or company_currency
    else:
        commission_currency = company_currency

    if commission_currency == company_currency:
        rate = 1.0
    elif commission_currency == txn_currency:
        if not exchange_rate or flt(exchange_rate) <= 0:
            frappe.throw(
                _("Cannot resolve exchange rate from {0} to {1}.").format(
                    txn_currency, company_currency
                )
            )
        rate = flt(exchange_rate)
    else:
        # Fixed (third) currency: resolve actual exchange rate.
        try:
            from frappe.utils import get_exchange_rate
            r = get_exchange_rate(
                commission_currency, company_currency, getattr(doc, "posting_date", None)
            )
            if not r or flt(r) <= 0:
                raise ValueError("no rate")
            rate = flt(r)
        except Exception:
            frappe.throw(
                _("Cannot resolve exchange rate from {0} to {1}.").format(
                    commission_currency, company_currency
                )
            )
    return commission_currency, (rate or 1.0)


def _expand_sales_person(sales_person):
    """Return list of sales person names.

    If sales_person is a group (is_group=1), expand to all descendants
    using the nested-set lft/rgt tree. Otherwise return [sales_person].
    """
    if not sales_person:
        return []
    try:
        is_group = frappe.get_cached_value("Sales Person", sales_person, "is_group")
    except Exception:
        is_group = 0
    if not is_group:
        return [sales_person]
    try:
        lft, rgt = frappe.get_cached_value("Sales Person", sales_person, ["lft", "rgt"])
    except Exception:
        return [sales_person]
    if not lft or not rgt:
        return [sales_person]
    try:
        descendants = frappe.get_all(
            "Sales Person",
            filters={"lft": [">=", lft], "rgt": ["<=", rgt]},
            pluck="name",
        )
    except Exception:
        return [sales_person]
    return descendants or [sales_person]


def _is_sales_person_group(name):
    """Return True if the given Sales Person is a group (is_group == 1)."""
    if not name:
        return False
    try:
        return bool(frappe.get_cached_value("Sales Person", name, "is_group"))
    except Exception:
        return False


def _resolve_posted_cost(item, company, company_currency):
    """Attempt to resolve actual posted cost for an item row from Stock Ledger Entry.
    Returns cost in company currency (positive outgoing cost), or None if no traceable source exists."""
    item_code = getattr(item, "item_code", None)
    if not item_code:
        return None
    is_stock = frappe.get_cached_value("Item", item_code, "is_stock_item")
    if not is_stock:
        return None
    voucher_no = getattr(item, "_parent", None) or getattr(item, "parent", None)
    if not voucher_no:
        return None
    # Cache the delivery_note lookup per voucher (request-scoped) so we don't
    # query Sales Invoice once per item row.
    cache = getattr(frappe.local, "commission_dn_cache", None)
    if cache is None:
        cache = {}
        try:
            frappe.local.commission_dn_cache = cache
        except Exception:
            cache = None
    dn = None
    if cache is not None and voucher_no in cache:
        dn = cache[voucher_no]
    else:
        try:
            dn = frappe.db.get_value("Sales Invoice", voucher_no, "delivery_note")
        except Exception:
            dn = None
        if cache is not None:
            cache[voucher_no] = dn
    # Prefer the Delivery Note as the posted-cost source when the invoice is linked
    # to one; otherwise use the Sales Invoice's own stock movement. Never sum
    # both sources, to avoid double-counting the same outgoing movement.
    if dn:
        voucher_types = ["Delivery Note"]
        voucher_no_for_lookup = dn
    else:
        voucher_types = ["Sales Invoice"]
        voucher_no_for_lookup = voucher_no

    for vt in voucher_types:
        try:
            # Only outgoing movements (actual_qty < 0) that are not cancelled.
            sle_rows = frappe.get_all(
                "Stock Ledger Entry",
                filters={
                    "voucher_type": vt,
                    "voucher_no": voucher_no_for_lookup,
                    "item_code": item_code,
                    "is_cancelled": 0,
                },
                fields=["stock_value_difference", "actual_qty"],
            )
            total_cost = 0.0
            found = False
            for r in sle_rows:
                if flt(r.get("actual_qty")) >= 0:
                    continue
                total_cost += abs(flt(r.get("stock_value_difference")))
                found = True
            if found:
                return flt(total_cost, 6)
        except Exception:
            continue
    # Fallback: try by voucher_detail_no (Delivery Note line linkage)
    try:
        sle_rows = frappe.get_all(
            "Stock Ledger Entry",
            filters={
                "voucher_detail_no": getattr(item, "name", None),
                "item_code": item_code,
                "is_cancelled": 0,
            },
            fields=["stock_value_difference", "actual_qty"],
        )
        total_cost = 0.0
        found = False
        for r in sle_rows:
            if flt(r.get("actual_qty")) >= 0:
                continue
            total_cost += abs(flt(r.get("stock_value_difference")))
            found = True
        if found:
            return flt(total_cost, 6)
    except Exception:
        pass
    return None


def _is_return(doc):
    return bool(getattr(doc, "is_return", 0))


def _event_key(
    source_doctype,
    source_name,
    source_row,
    plan_name,
    pool_code,
    beneficiary_type,
    beneficiary,
    event_type,
):
    return "|".join(
        [
            source_doctype or "",
            source_name or "",
            source_row or "",
            plan_name or "",
            pool_code or "",
            beneficiary_type or "",
            beneficiary or "",
            event_type or "",
        ]
    )


def _item_group_path(item_group):
    """Return list of ancestor item groups from root to leaf (deepest last)."""
    path = []
    current = item_group
    seen = set()
    while current and current not in seen:
        seen.add(current)
        path.append(current)
        try:
            parent = frappe.get_cached_value("Item Group", current, "parent_item_group")
        except Exception:
            parent = None
        if not parent or parent == current:
            break
        current = parent
    path.reverse()
    return path


def _resolve_plan(customer, customer_group, territory, company, posting_date):
    """Resolve applicable Commission Plan using scope precedence and priority."""
    candidates = frappe.get_all(
        "Commission Plan",
        filters={"enabled": 1, "company": company},
        fields=[
            "name",
            "applies_to",
            "customer",
            "customer_group",
            "territory",
            "priority",
            "valid_from",
            "valid_to",
        ],
    )
    valid = []
    for p in candidates:
        vf = p.get("valid_from")
        vt = p.get("valid_to")
        if vf and posting_date and getdate(posting_date) < getdate(vf):
            continue
        if vt and posting_date and getdate(posting_date) > getdate(vt):
            continue
        # scope match
        scope = p.get("applies_to")
        if scope == "Specific Customer":
            if p.get("customer") != customer:
                continue
        elif scope == "Customer Group":
            if p.get("customer_group") != customer_group:
                continue
        elif scope == "Territory":
            if p.get("territory") != territory:
                continue
        elif scope == "All":
            pass
        else:
            continue
        valid.append(p)

    if not valid:
        return None

    # scope precedence: Specific Customer > Customer Group > Territory > All
    scope_rank = {"Specific Customer": 0, "Customer Group": 1, "Territory": 2, "All": 3}
    valid.sort(
        key=lambda p: (
            scope_rank.get(p.get("applies_to"), 9),
            -flt(p.get("priority") or 0),
        )
    )
    return frappe.get_doc("Commission Plan", valid[0]["name"])


def _match_rule(plan, item_code, item_group):
    """Match most specific rule: Item > deepest Item Group > All Items.

    Within same specificity, higher priority wins. Ambiguous ties raise.
    """
    rules = [r for r in (plan.get("rules") or []) if getattr(r, "enabled", 1)]
    if not rules:
        return None

    # Group 1: exact item
    item_rules = [
        r for r in rules if r.match_type == "Item" and r.item_code == item_code
    ]
    if item_rules:
        if len(item_rules) > 1:
            priorities = {flt(r.priority or 0) for r in item_rules}
            if len(priorities) == 1:
                frappe.throw(
                    _(
                        "Ambiguous commission rules: multiple Item rules with equal priority for {0}"
                    ).format(item_code)
                )
        item_rules.sort(key=lambda r: -flt(r.priority or 0))
        return item_rules[0]

    # Group 2: item group (deepest path)
    if item_group:
        path = _item_group_path(item_group)
        # try from deepest to shallowest
        for group in reversed(path):
            grp_rules = [
                r
                for r in rules
                if r.match_type == "Item Group" and r.item_group == group
            ]
            if grp_rules:
                if len(grp_rules) > 1:
                    priorities = {flt(r.priority or 0) for r in grp_rules}
                    if len(priorities) == 1:
                        frappe.throw(
                            _(
                                "Ambiguous commission rules: multiple Item Group rules with equal priority for {0}"
                            ).format(group)
                        )
                grp_rules.sort(key=lambda r: -flt(r.priority or 0))
                return grp_rules[0]

    # Group 3: all items
    all_rules = [r for r in rules if r.match_type == "All Items"]
    if all_rules:
        if len(all_rules) > 1:
            priorities = {flt(r.priority or 0) for r in all_rules}
            if len(priorities) == 1:
                frappe.throw(
                    _(
                        "Ambiguous commission rules: multiple All Items rules with equal priority"
                    )
                )
        all_rules.sort(key=lambda r: -flt(r.priority or 0))
        return all_rules[0]

    return None


def _calculate_basis(item, rule, doc):
    """Return (basis_name, basis_company, basis_txn, source_qty, source_uom)."""
    company = doc.company
    company_currency = _company_currency(company)
    net_company = flt(getattr(item, "base_net_amount", None))
    if not net_company:
        net_company = flt(item.net_amount)
    net_txn = flt(item.net_amount)

    valuation_rate = flt(getattr(item, "valuation_rate", 0))

    basis = (
        getattr(rule, "basis", None)
        or getattr(rule, "calculation_basis", None)
        or "Net Total"
    )
    if basis == "Gross Profit":
        # Attempt to use actual posted cost from Stock Ledger Entry when available.
        # Fall back to valuation_rate only as a documented best-effort; flag if
        # no cost source can be traced so finance can review.
        cogs_company = _resolve_posted_cost(item, doc.company, company_currency)
        if cogs_company is None:
            conversion_factor = flt(getattr(item, "conversion_factor", 1) or 1)
            cogs_company = valuation_rate * flt(item.qty) * conversion_factor
            frappe.log_error(
                "No posted cost source found for item {} on {}; falling back to valuation_rate.".format(
                    getattr(item, "item_code", ""), getattr(doc, "name", "")
                ),
                "Commission Gross Profit Cost Source",
            )
        exchange_rate = flt(getattr(doc, "conversion_rate", 1)) or 1.0
        cogs_txn = cogs_company / exchange_rate if exchange_rate else cogs_company
        basis_company = net_company - cogs_company
        basis_txn = net_txn - cogs_txn
        basis_name = "Gross Profit"
    elif basis in ("Fixed Amount", "Fixed"):
        basis_company = flt(item.qty) * flt(getattr(rule, "fixed_amount", 0))
        basis_txn = flt(item.qty) * flt(getattr(rule, "fixed_amount", 0))
        basis_name = "Fixed Amount"
    else:
        basis_company = net_company
        basis_txn = net_txn
        basis_name = "Net Total"

    if basis_company < 0 and not getattr(rule, "allow_negative_basis", 0):
        return basis_name, 0.0, 0.0, flt(item.qty), getattr(item, "uom", None)
    if basis_company == 0:
        return basis_name, 0.0, 0.0, flt(item.qty), getattr(item, "uom", None)

    return (
        basis_name,
        flt(basis_company, 6),
        flt(basis_txn, 6),
        flt(item.qty),
        getattr(item, "uom", None),
    )


def _apply_floor_cap(amount, rule):
    min_c = flt(getattr(rule, "min_commission", 0))
    max_c = flt(getattr(rule, "max_commission", 0))
    if min_c and amount < min_c:
        amount = min_c
    if max_c and amount > max_c:
        amount = max_c
    return amount


def _entries_exist(source_name, event_type=None):
    filters = {"sales_invoice": source_name, "docstatus": ["!=", 2]}
    if event_type:
        filters["event_type"] = event_type
    return frappe.db.exists("Commission Entry", filters)


def _insert_entry(
    doc,
    plan,
    rule,
    item,
    beneficiary_type,
    beneficiary,
    pool_code,
    basis_name,
    basis_company,
    basis_txn,
    source_qty,
    source_uom,
    item_commission,
    split_share,
    company_currency,
    txn_currency,
    exchange_rate,
    commission_currency=None,
    commission_to_company_rate=None,
    selected_slab_rate=0.0,
    attainment_before_pct=0.0,
    attainment_after_pct=0.0,
    event_type="Invoice Earned",
):
    """Persist a Commission Entry with idempotency key."""
    item_code = getattr(item, "item_code", None)
    item_group = getattr(item, "item_group", None)
    source_row = getattr(item, "name", None)

    # resolve employee/supplier snapshot
    employee = None
    supplier = None
    if beneficiary_type == "Sales Person":
        try:
            employee = frappe.get_cached_value("Sales Person", beneficiary, "employee")
        except Exception:
            employee = None
    elif beneficiary_type == "Sales Partner":
        try:
            supplier = frappe.get_cached_value("Sales Partner", beneficiary, "supplier")
        except Exception:
            supplier = None

    # Resolve commission currency and rate (passed from caller or fallback).
    if commission_currency is None:
        policy = getattr(plan, "commission_currency_policy", None) or "Company Currency"
        if policy == "Transaction Currency":
            commission_currency = txn_currency or company_currency
        elif policy == "Fixed Currency":
            commission_currency = getattr(plan, "commission_currency", None) or company_currency
        else:
            commission_currency = company_currency
    if commission_to_company_rate is None:
        if commission_currency == company_currency:
            commission_to_company_rate = 1.0
        elif commission_currency == txn_currency:
            commission_to_company_rate = exchange_rate
        else:
            try:
                from frappe.utils import get_exchange_rate
                rate = get_exchange_rate(
                    commission_currency, company_currency, getattr(doc, "posting_date", None)
                )
                if not rate or flt(rate) <= 0:
                    raise ValueError("no rate")
                commission_to_company_rate = flt(rate)
            except Exception:
                frappe.throw(
                    _("Cannot resolve exchange rate from {0} to {1}.").format(
                        commission_currency, company_currency
                    )
                )

    commission_rate = flt(getattr(rule, "rate_percentage", 0))
    fixed_amount = flt(getattr(rule, "fixed_amount", 0))
    # Store the calculation method from the actual Commission Rule.
    calculation_method = getattr(rule, "calculation_method", None) or "Percentage"

    # Commission-to-company conversion rate snapshot.
    if commission_currency == company_currency:
        commission_to_company_rate = 1.0
    elif commission_currency == txn_currency:
        commission_to_company_rate = exchange_rate
    else:
        # Fixed (third) currency: resolve actual exchange rate.
        try:
            from frappe.utils import get_exchange_rate
            rate = get_exchange_rate(
                commission_currency, company_currency, getattr(doc, "posting_date", None)
            )
            if not rate or flt(rate) <= 0:
                raise ValueError("no rate")
            commission_to_company_rate = flt(rate)
        except Exception:
            frappe.throw(
                _("Cannot resolve exchange rate from {0} to {1}.").format(
                    commission_currency, company_currency
                )
            )

    ev_key = _event_key(
        "Sales Invoice",
        doc.name,
        source_row,
        plan.name,
        pool_code,
        beneficiary_type,
        beneficiary,
        event_type,
    )

    # idempotency: return existing (non-cancelled) entry name if already present
    existing = frappe.db.get_value(
        "Commission Entry", {"event_key": ev_key, "docstatus": ["!=", 2]}, "name"
    )
    if existing:
        return existing

    entry = frappe.get_doc(
        {
            "doctype": "Commission Entry",
            "event_key": ev_key,
            "event_type": event_type,
            "company": doc.company,
            "posting_date": doc.posting_date,
            "source_doctype": "Sales Invoice",
            "source_name": doc.name,
            "source_row": source_row,
            "original_invoice": doc.name,
            "plan": plan.name,
            "rule": getattr(rule, "name", None),
            "pool_code": pool_code,
            "beneficiary_type": beneficiary_type,
            "sales_person": beneficiary if beneficiary_type == "Sales Person" else None,
            "sales_partner": beneficiary
            if beneficiary_type == "Sales Partner"
            else None,
            "employee": employee,
            "supplier": supplier,
            "item_code": item_code,
            "item_name": getattr(item, "item_name", None),
            "item_group": item_group,
            "source_qty": flt(source_qty),
            "source_uom": source_uom,
            "commission_qty": flt(source_qty),
            "commission_uom": getattr(rule, "commission_uom", None) or source_uom,
            "transaction_currency": txn_currency,
            "transaction_basis_amount": flt(basis_txn, 6),
            "company_currency": company_currency,
            "company_basis_amount": flt(basis_company, 6),
            "conversion_rate_snapshot": flt(exchange_rate),
            "commission_currency": commission_currency,
            "commission_rate": commission_rate,
            "commission_to_company_rate": commission_to_company_rate,
            "fixed_amount_snapshot": fixed_amount,
            "earned_delta": flt(item_commission, 6),
            "payable_eligible_delta": 0,
            "reserved_delta": 0,
            "paid_delta": 0,
            "sales_invoice": doc.name,
            "commission_basis": basis_name,
            "commission_basis_amount": flt(basis_company * split_share, 6),
            "exchange_rate": exchange_rate,
            "earned_commission": flt(item_commission, 2),
            "payable_amount": 0,
            "collection_eligible_amount": 0,
            "settled_amount": 0,
            "paid_amount": 0,
            "outstanding_amount": 0,
            "status": "Earned",
            "accrual_status": "Not Accrued",
            "collection_status": "Not Eligible",
            "settlement_status": "Not Reserved",
            "remarks": "",
            "calculation_snapshot": frappe.as_json(
                {
                    "plan": plan.name,
                    "rule": getattr(rule, "name", None),
                    "calculation_method": calculation_method,
                    "rate_percentage": flt(getattr(rule, "rate_percentage", 0)),
                    "fixed_amount": flt(getattr(rule, "fixed_amount", 0)),
                    "basis_name": basis_name,
                    "basis_company": basis_company,
                    "basis_txn": basis_txn,
                    "split_share": split_share,
                    "item_commission": item_commission,
                    "conversion_rate": exchange_rate,
                    "commission_currency": commission_currency,
                    "effective_rate_percentage": (
                        (flt(item_commission) / flt(basis_company) * 100.0)
                        if flt(basis_company)
                        else 0.0
                    ),
                    "selected_slab_rate": flt(selected_slab_rate),
                    "contributor_split_percentage": flt(split_share * 100.0),
                    "effective_rate_after_split": (
                        (flt(item_commission) / flt(basis_company) * 100.0)
                        if flt(basis_company)
                        else 0.0
                    ),
                    "attainment_before_pct": flt(attainment_before_pct, 6),
                    "attainment_after_pct": flt(attainment_after_pct, 6),
                    "slab_calculation_method": getattr(
                        plan, "slab_calculation_method", None
                    ),
                }
            ),
        }
    )
    try:
        entry.insert(ignore_permissions=True)
    except frappe.DuplicateEntryError:
        existing = frappe.db.get_value(
            "Commission Entry", {"event_key": ev_key, "docstatus": ["!=", 2]}, "name"
        )
        if existing:
            return existing
        raise
    entry.submit()
    return entry.name


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def _native_overlap_guard(doc):
    """Warn when native ERPNext commission rates are set on the invoice's sales team
    while the custom engine is configured to block native overlap."""
    try:
        if not frappe.db.exists("Commission Settings"):
            return
        settings = frappe.get_single("Commission Settings")
    except Exception:
        return
    if not getattr(settings, "block_native_hrms_overlap", 0):
        return
    native_found = False
    for row in getattr(doc, "sales_team", None) or []:
        if (
            flt(getattr(row, "commission_rate", 0)) > 0
            or flt(getattr(row, "incentives", 0)) > 0
        ):
            native_found = True
    if (
        getattr(doc, "sales_partner", None)
        and flt(getattr(doc, "total_commission", 0)) > 0
    ):
        native_found = True
    if native_found:
        frappe.log_error(
            "Native commission fields detected on Sales Invoice {}; "
            "before_submit neutralization should have cleared them.".format(
                getattr(doc, "name", "")
            ),
            "Commission Native Overlap Guard",
        )


def neutralize_native_commission(doc, method=None):
    """Mutate the (not-yet-submitted) Sales Invoice in memory so native ERPNext
    commission fields are not persisted when block_native_hrms_overlap is set.
    Runs as before_submit; Frappe persists the modified values."""
    try:
        if not frappe.db.exists("Commission Settings"):
            return
        settings = frappe.get_single("Commission Settings")
    except Exception:
        return
    if not getattr(settings, "block_native_hrms_overlap", 0):
        return
    for row in getattr(doc, "sales_team", None) or []:
        try:
            row.commission_rate = 0
            row.incentives = 0
        except Exception:
            pass
    if getattr(doc, "sales_partner", None):
        try:
            doc.total_commission = 0
        except Exception:
            pass


def neutralize_native_commission_on_update(doc, method=None):
    """Mutate an already-submitted Sales Invoice in memory so native ERPNext
    commission fields are not recalculated when the Sales Team is edited after
    submission. Runs as before_update_after_submit."""
    try:
        if not frappe.db.exists("Commission Settings"):
            return
        settings = frappe.get_single("Commission Settings")
    except Exception:
        return
    if not getattr(settings, "block_native_hrms_overlap", 0):
        return
    for row in getattr(doc, "sales_team", None) or []:
        try:
            row.commission_rate = 0
            row.incentives = 0
        except Exception:
            pass
    if getattr(doc, "sales_partner", None):
        try:
            doc.total_commission = 0
        except Exception:
            pass


def _source_enabled(doc):
    """Check if commission generation is enabled for the given source document."""
    if not frappe.db.exists("Commission Settings"):
        return True
    settings = frappe.get_single("Commission Settings")
    if not getattr(settings, "enabled", 1):
        return False
    dt = getattr(doc, "doctype", None)
    if dt == "Sales Invoice" and not getattr(settings, "enable_sales_invoice", 1):
        return False
    if dt == "POS Invoice" and not getattr(settings, "enable_pos_invoice", 1):
        return False
    return True


def generate_commission_entries(doc, method=None, force=False):
    """Triggered on Sales Invoice / POS Invoice on_submit. Creates Commission Entry records."""
    if not _source_enabled(doc):
        return
    if _is_return(doc):
        handle_return_invoice(doc, method)
        return

    # idempotency at document level
    if _entries_exist(doc.name):
        return

    # native commission overlap guard
    _native_overlap_guard(doc)

    plan = _resolve_plan(
        getattr(doc, "customer", None),
        getattr(doc, "customer_group", None),
        getattr(doc, "territory", None),
        getattr(doc, "company", None),
        getattr(doc, "posting_date", None),
    )
    if not plan:
        return

    # Honor the plan's earning_event unless forced (e.g. by Payment Entry
    # for "On Payment" plans).
    earning_event = getattr(plan, "earning_event", None) or "On Invoice Submit"
    if not force and earning_event != "On Invoice Submit":
        return

    # contributors: prefer plan contributors, else invoice sales team
    plan_contributors = plan.get("contributors") or []
    if plan_contributors:
        contributors = []
        for pc in plan_contributors:
            contributors.append(
                {
                    "sales_person": getattr(pc, "sales_person", None),
                    "sales_partner": getattr(pc, "sales_partner", None),
                    "contributor_type": getattr(pc, "contributor_type", "Sales Person"),
                    "allocated_percentage": flt(getattr(pc, "split_percentage", 100.0)),
                    "commission_rate": flt(getattr(pc, "commission_rate", 0)),
                    "pool_code": getattr(pc, "pool_code", None) or "INTERNAL_SALES",
                    "_from_plan": True,
                }
            )
    else:
        contributors = []
        for st in doc.get("sales_team") or []:
            contributors.append(
                {
                    "sales_person": getattr(st, "sales_person", None),
                    "sales_partner": None,
                    "contributor_type": "Sales Person",
                    "allocated_percentage": flt(getattr(st, "allocated_percentage", 0)),
                    "commission_rate": flt(getattr(st, "commission_rate", 0)),
                    "pool_code": "INTERNAL_SALES",
                    "_from_plan": False,
                }
            )

    # Exclude group sales persons: a group is a non-earning container, not a
    # beneficiary. Only leaf (individual) sales persons earn commission entries.
    filtered_contributors = []
    for _rep in contributors:
        if (
            _rep.get("contributor_type") or "Sales Person"
        ) == "Sales Person" and _is_sales_person_group(_rep.get("sales_person")):
            frappe.logger("sales_commission_management").warning(
                "Excluded group sales person '%s' from commission generation for %s",
                _rep.get("sales_person"),
                getattr(doc, "name", None),
            )
            continue
        filtered_contributors.append(_rep)
    contributors = filtered_contributors

    external_partner = getattr(doc, "sales_partner", None)
    flt(getattr(doc, "commission_rate", 0))

    if not contributors and not external_partner:
        return

    company_currency = _company_currency(doc.company)
    txn_currency = getattr(doc, "currency", None) or company_currency
    exchange_rate = flt(getattr(doc, "conversion_rate", 1)) or 1.0

    # Resolve commission currency and conversion rate once for this invoice.
    commission_currency, commission_to_company_rate = _resolve_commission_currency(
        plan, doc, company_currency, txn_currency, exchange_rate
    )

    slabs = plan.get("slabs") or []
    slab_method = getattr(plan, "slab_calculation_method", None) or "Progressive"

    fixed_invoice_rules_applied = set()

    # Plan-level target value and cumulative target metric for the period.
    _target_value = _plan_target_value(plan)
    _db_cumulative_metric = _plan_cumulative_metric(
        plan, doc.company, getattr(doc, "posting_date", None)
    )
    _running_current_metric = [0.0]

    for item in doc.items or []:
        _selected_slab_rate_val = 0.0
        _prev_att_item = 0.0
        _after_att_item = 0.0
        rule = _match_rule(plan, item.item_code, item.item_group)
        if not rule:
            continue

        # grant_commission check
        if getattr(rule, "exclude_if_grant_commission_zero", 1) and not getattr(
            item, "grant_commission", 1
        ):
            continue

        # include_free_items check
        if (
            not getattr(rule, "include_free_items", 0)
            and flt(item.rate or item.price_list_rate or 0) == 0
        ):
            # free item
            if flt(item.net_amount) == 0:
                continue

        basis_name, basis_company, basis_txn, source_qty, source_uom = _calculate_basis(
            item, rule, doc
        )
        if basis_company <= 0:
            continue

        calc_method = getattr(rule, "calculation_method", None) or "Percentage"
        item_commission = 0.0

        if calc_method == "Fixed Per Unit":
            item_commission = flt(getattr(rule, "fixed_amount", 0)) * flt(item.qty)
        elif calc_method == "Fixed Per Line":
            item_commission = flt(getattr(rule, "fixed_amount", 0))
        elif calc_method == "Fixed Per Invoice":
            # Apply once per rule per invoice, regardless of item order.
            if getattr(rule, "name", None) not in fixed_invoice_rules_applied:
                item_commission = flt(getattr(rule, "fixed_amount", 0))
                fixed_invoice_rules_applied.add(getattr(rule, "name", None))
        elif getattr(rule, "use_slabs", 0) and slabs:
            if _target_value and _target_value > 0:
                _prev_att = (
                    (_db_cumulative_metric + _running_current_metric[0])
                    / _target_value
                    * 100.0
                )
                _after_att = (
                    (_db_cumulative_metric + _running_current_metric[0] + basis_company)
                    / _target_value
                    * 100.0
                )
            else:
                _prev_att = 0.0
                _after_att = 0.0
            _prev_att_item = _prev_att
            _after_att_item = _after_att
            _selected_slab_rate_val = _selected_slab_rate(slabs, slab_method, _after_att)
            item_commission = _calculate_slab_amount(
                basis_company,
                slabs,
                slab_method,
                attainment_pct=_after_att,
                prev_attainment_pct=_prev_att,
                target_value=_target_value,
            )
        else:
            rate = flt(getattr(rule, "rate_percentage", 0))
            if rate == 0 and not flt(getattr(rule, "fixed_amount", 0)):
                if getattr(rule, "basis", None) in ("Net Total", "Gross Profit"):
                    continue
            item_commission = basis_company * (rate / 100.0)
            if flt(getattr(rule, "fixed_amount", 0)):
                item_commission += flt(getattr(rule, "fixed_amount", 0)) * flt(item.qty)

        if item_commission <= 0 and not getattr(rule, "allow_negative_basis", 0):
            continue

        item_commission = _apply_floor_cap(item_commission, rule)

        # Convert from company currency to the selected commission currency.
        if commission_to_company_rate and commission_to_company_rate != 1.0:
            item_commission = flt(item_commission) / commission_to_company_rate

        # Accumulate this item's basis into the period's running metric so
        # subsequent items see the updated attainment.
        _running_current_metric[0] += basis_company

        for rep in contributors:
            split_share = (flt(rep.get("allocated_percentage", 0)) or 0) / 100.0
            if split_share <= 0:
                continue
            ctype = rep.get("contributor_type") or "Sales Person"
            pool_code = rep.get("pool_code") or "INTERNAL_SALES"

            if ctype == "Sales Partner":
                partner = rep.get("sales_partner")
                if not partner:
                    continue
                # Use the common calculation pipeline; do not skip fixed/slab
                # rules whose base rate_percentage is zero.
                rep_commission = item_commission * split_share
                if rep_commission <= 0 and not flt(getattr(rule, "fixed_amount", 0)):
                    continue
                _insert_entry(
                    doc,
                    plan,
                    rule,
                    item,
                    "Sales Partner",
                    partner,
                    pool_code,
                    basis_name,
                    basis_company,
                    basis_txn,
                    source_qty,
                    source_uom,
                    rep_commission,
                    split_share,
                    company_currency,
                    txn_currency,
                    exchange_rate,
                    commission_currency=commission_currency,
                    commission_to_company_rate=commission_to_company_rate,
                    selected_slab_rate=_selected_slab_rate_val,
                    attainment_before_pct=_prev_att_item,
                    attainment_after_pct=_after_att_item,
                )
                continue

            # Plan rule rate is the single commission-rate source; do not silently
            # substitute the native Sales Person master rate.
            rep_commission = item_commission * split_share

            if rep_commission <= 0:
                continue

            beneficiary = rep.get("sales_person")
            if not beneficiary:
                continue
            _insert_entry(
                doc,
                plan,
                rule,
                item,
                "Sales Person",
                beneficiary,
                pool_code,
                basis_name,
                basis_company,
                basis_txn,
                source_qty,
                source_uom,
                rep_commission,
                split_share,
                company_currency,
                txn_currency,
                exchange_rate,
                commission_currency=commission_currency,
                commission_to_company_rate=commission_to_company_rate,
                selected_slab_rate=_selected_slab_rate_val,
                attainment_before_pct=_prev_att_item,
                attainment_after_pct=_after_att_item,
            )

        # External sales partner from invoice. Skip if the same partner is already
        # a contributor in the plan, to avoid double-counting.
        plan_partner_beneficiaries = {
            getattr(pc, "sales_partner", None)
            for pc in (plan.get("contributors") or [])
            if getattr(pc, "contributor_type", "") == "Sales Partner"
        }
        if external_partner and external_partner not in plan_partner_beneficiaries:
            # Use the common calculation pipeline (item_commission), not a
            # percentage-only formula, so fixed/slab/currency conversion apply.
            partner_commission = item_commission
            if partner_commission > 0:
                _insert_entry(
                    doc,
                    plan,
                    rule,
                    item,
                    "Sales Partner",
                    external_partner,
                    "EXTERNAL_PARTNER",
                    basis_name,
                    basis_company,
                    basis_txn,
                    source_qty,
                    source_uom,
                    partner_commission,
                    1.0,
                    company_currency,
                    txn_currency,
                    exchange_rate,
                    commission_currency=commission_currency,
                    commission_to_company_rate=commission_to_company_rate,
                    selected_slab_rate=_selected_slab_rate_val,
                    attainment_before_pct=_prev_att_item,
                    attainment_after_pct=_after_att_item,
                )


def _calculate_slab_amount(
    basis_amount,
    slabs,
    method="Progressive",
    attainment_pct=0.0,
    prev_attainment_pct=0.0,
    target_value=0.0,
):
    """Calculate commission for an invoice using attainment-based slabs.

    Slabs are defined in attainment-percentage bands (min_attainment /
    max_attainment). ``attainment_pct`` is the cumulative target attainment
    including this invoice. ``prev_attainment_pct`` is the attainment before
    this invoice (used for progressive marginal calculation).
    """
    if not slabs:
        return 0.0

    def _from(s):
        return flt(getattr(s, "min_attainment", None) or getattr(s, "from_amount", 0))

    def _to(s):
        v = getattr(s, "max_attainment", None)
        if v is None:
            v = getattr(s, "to_amount", None)
        return flt(v) if v not in (None, 0) else float("inf")

    def _rate(s):
        return flt(getattr(s, "rate_percentage", 0))

    def _fixed(s):
        return flt(getattr(s, "fixed_bonus", None) or getattr(s, "fixed_amount", 0))

    slabs = sorted(slabs, key=_from)

    if method == "Single":
        applicable = None
        for slab in slabs:
            if _from(slab) <= attainment_pct < _to(slab):
                applicable = slab
                break
        if applicable is None and slabs:
            if attainment_pct > _from(slabs[-1]):
                applicable = slabs[-1]
            else:
                applicable = slabs[0]
        if not applicable:
            return 0.0
        rate = _rate(applicable) / 100.0
        fixed = _fixed(applicable)
        return flt(basis_amount * rate + fixed, 2)

    # Progressive: marginal rates across attainment bands crossed by this invoice.
    before = flt(prev_attainment_pct or 0.0)
    after = flt(attainment_pct or 0.0)
    if after < before:
        after = before
    if after <= before:
        # No attainment movement; fall back to single rate at current attainment.
        applicable = None
        for slab in slabs:
            if _from(slab) <= after <= _to(slab):
                applicable = slab
                break
        if applicable is None and slabs:
            applicable = slabs[-1] if after > _from(slabs[-1]) else slabs[0]
        if not applicable:
            return 0.0
        return flt(basis_amount * (_rate(applicable) / 100.0) + _fixed(applicable), 2)

    total_change = after - before
    total = 0.0
    for idx, slab in enumerate(slabs):
        slab_to = float("inf") if idx == len(slabs) - 1 else _to(slab)
        lo = max(_from(slab), before)
        hi = min(slab_to, after)
        if hi <= lo:
            continue
        band_width = hi - lo
        portion = basis_amount * (band_width / total_change) if total_change else 0.0
        rate = _rate(slab) / 100.0
        total += portion * rate
    # Apply fixed bonus only when this invoice crosses into a slab, so a
    # fixed bonus is not re-applied on every subsequent invoice in the same slab.
    for idx, slab in enumerate(slabs):
        crossed = False
        if _from(slab) == 0:
            if idx == 0 and before == 0 and after > 0:
                crossed = True
        else:
            if before < _from(slab) <= after:
                crossed = True
        if crossed:
            if _fixed(slab):
                total += _fixed(slab)
            break
    return flt(total, 2)


def _selected_slab_rate(slabs, method, attainment_pct):
    """Return the rate percentage of the slab applicable to attainment_pct."""
    if not slabs:
        return 0.0

    def _from(s):
        return flt(getattr(s, "min_attainment", None) or getattr(s, "from_amount", 0))

    def _to(s):
        v = getattr(s, "max_attainment", None)
        if v is None:
            v = getattr(s, "to_amount", None)
        return flt(v) if v not in (None, 0) else float("inf")

    slabs = sorted(slabs, key=_from)
    attainment_pct = flt(attainment_pct or 0.0)
    applicable = None
    for slab in slabs:
        if _from(slab) <= attainment_pct < _to(slab):
            applicable = slab
            break
    if applicable is None and slabs:
        if attainment_pct > _from(slabs[-1]):
            applicable = slabs[-1]
        else:
            applicable = slabs[0]
    if not applicable:
        return 0.0
    return flt(getattr(applicable, "rate_percentage", 0))


def generate_commission_on_payment(payment_entry, method=None):
    """Generate commission entries for invoices whose plan uses
    earning_event = 'On Payment', triggered by Payment Entry submit.

    Entries are only created if they do not already exist for the invoice.
    """
    # Validate the payment entry is a genuine customer receipt.
    if getattr(payment_entry, "payment_type", None) not in (None, "Receive"):
        return
    if getattr(payment_entry, "party_type", None) not in (None, "Customer"):
        return
    if getattr(payment_entry, "docstatus", 0) != 1:
        return
    # Check settings.
    if frappe.db.exists("Commission Settings"):
        settings = frappe.get_single("Commission Settings")
        if not getattr(settings, "enabled", 1):
            return
        if not getattr(settings, "enable_payment_entry", 1):
            return

    references = payment_entry.get("references") or []
    invoices = set()
    for ref in references:
        if ref.reference_doctype == "Sales Invoice" and ref.reference_name:
            if flt(getattr(ref, "allocated_amount", 0)) <= 0:
                continue
            invoices.add(ref.reference_name)

    for invoice_name in invoices:
        try:
            inv = frappe.get_doc("Sales Invoice", invoice_name)
        except Exception:
            continue
        # resolve plan
        plan = _resolve_plan(
            getattr(inv, "customer", None),
            getattr(inv, "customer_group", None),
            getattr(inv, "territory", None),
            getattr(inv, "company", None),
            getattr(inv, "posting_date", None),
        )
        if not plan:
            continue
        earning_event = getattr(plan, "earning_event", None) or "On Invoice Submit"
        if earning_event != "On Payment":
            continue
        if _entries_exist(inv.name):
            continue
        # generate (force=True bypasses the earning_event check)
        generate_commission_entries(inv, method=method, force=True)


def _get_row_stock_qty(item):
    """Get stock quantity for a Sales Invoice Item row."""
    sq = flt(getattr(item, "stock_qty", 0))
    if sq:
        return abs(sq)
    qty = abs(flt(getattr(item, "qty", 0)))
    cf = flt(getattr(item, "conversion_factor", 0)) or 1.0
    return qty * cf


def _build_return_allocations(return_doc, original_doc):
    """Map returned quantities and amounts to exact original invoice item rows.

    Uses Sales Invoice Item.sales_invoice_item to identify the original row.
    Never allocates by item_code alone.
    """
    original_items = {row.name: row for row in (original_doc.items or [])}
    allocations = {}
    for returned_item in return_doc.items or []:
        original_row_name = getattr(returned_item, "sales_invoice_item", None)
        if not original_row_name:
            frappe.throw(
                _(
                    "Return row {0} is not linked to an original Sales Invoice "
                    "Item row. Please recreate or correct the return reference."
                ).format(returned_item.idx)
            )
        original_item = original_items.get(original_row_name)
        if not original_item:
            frappe.throw(
                _(
                    "Return row {0} references invalid original invoice row {1}."
                ).format(returned_item.idx, original_row_name)
            )
        if returned_item.item_code != original_item.item_code:
            frappe.throw(
                _("Item mismatch on return row {0}.").format(returned_item.idx)
            )
        original_stock_uom = original_item.get("stock_uom")
        return_stock_uom = returned_item.get("stock_uom")
        if (
            original_stock_uom
            and return_stock_uom
            and original_stock_uom != return_stock_uom
        ):
            frappe.throw(
                _("Stock UOM mismatch for original row {0}.").format(
                    original_row_name
                )
            )
        original_cf = flt(original_item.get("conversion_factor")) or 1.0
        if original_cf <= 0:
            frappe.throw(
                _("Invalid original conversion factor for row {0}.").format(
                    original_row_name
                )
            )
        returned_stock_qty = _get_row_stock_qty(returned_item)
        returned_qty = returned_stock_qty / original_cf
        if returned_qty <= 0:
            continue
        allocation = allocations.setdefault(
            original_row_name,
            {
                "qty": 0.0,
                "stock_qty": 0.0,
                "net_company": 0.0,
            },
        )
        allocation["qty"] += returned_qty
        allocation["stock_qty"] += returned_stock_qty
        allocation["net_company"] += abs(
            flt(returned_item.get("base_net_amount"))
        )
    return allocations


def _get_submitted_return_quantities(original_doc):
    """Get cumulative submitted return quantities by original row name.

    Uses submitted return invoices as the authoritative source.
    """
    return_names = frappe.get_all(
        "Sales Invoice",
        filters={
            "return_against": original_doc.name,
            "is_return": 1,
            "docstatus": 1,
        },
        pluck="name",
    )
    if not return_names:
        return {}
    returned_rows = frappe.get_all(
        "Sales Invoice Item",
        filters={
            "parent": ["in", return_names],
            "parenttype": "Sales Invoice",
        },
        fields=[
            "name",
            "item_code",
            "sales_invoice_item",
            "qty",
            "stock_qty",
            "conversion_factor",
        ],
    )
    totals = {}
    for row in returned_rows:
        source_row = row.get("sales_invoice_item")
        if not source_row:
            frappe.throw(
                _("Submitted return item {0} has no original row reference.").format(
                    row.name
                )
            )
        original_items = {r.name: r for r in (original_doc.items or [])}
        original_item = original_items.get(source_row)
        if not original_item or row.item_code != original_item.item_code:
            frappe.throw(
                _("Invalid original invoice row reference on return item {0}.").format(
                    row.name
                )
            )
        original_cf = flt(original_item.get("conversion_factor")) or 1.0
        if original_cf <= 0:
            frappe.throw(
                _("Invalid original conversion factor for row {0}.").format(
                    source_row
                )
            )
        returned_qty = _get_row_stock_qty(row) / original_cf
        totals[source_row] = totals.get(source_row, 0.0) + returned_qty
    return totals


def _is_original_invoice_fully_returned(original_doc, returned_totals):
    """Determine whether every original invoice item row is fully returned."""
    has_items = False
    for original_item in original_doc.items or []:
        original_qty = abs(flt(original_item.qty))
        if original_qty <= 0:
            continue
        has_items = True
        source_row = original_item.name
        returned_qty = returned_totals.get(source_row, 0.0)
        if returned_qty < original_qty - 0.0001:
            return False
    return has_items


def _get_submitted_return_allocations_cumulative(original_doc):
    """Cumulative submitted return allocations by original row name.

    Returns a dict mapping original row name -> {qty, stock_qty, net_company}
    across all submitted return invoices against original_doc.
    """
    return_names = frappe.get_all(
        "Sales Invoice",
        filters={
            "return_against": original_doc.name,
            "is_return": 1,
            "docstatus": 1,
        },
        pluck="name",
    )
    if not return_names:
        return {}
    returned_rows = frappe.get_all(
        "Sales Invoice Item",
        filters={
            "parent": ["in", return_names],
            "parenttype": "Sales Invoice",
        },
        fields=[
            "name",
            "item_code",
            "sales_invoice_item",
            "qty",
            "stock_qty",
            "conversion_factor",
            "base_net_amount",
        ],
    )
    original_items = {r.name: r for r in (original_doc.items or [])}
    totals = {}
    for row in returned_rows:
        source_row = row.get("sales_invoice_item")
        if not source_row:
            continue
        original_item = original_items.get(source_row)
        if not original_item or row.get("item_code") != original_item.item_code:
            continue
        original_cf = flt(original_item.get("conversion_factor")) or 1.0
        if original_cf <= 0:
            continue
        returned_stock_qty = _get_row_stock_qty(row)
        returned_qty = returned_stock_qty / original_cf
        allocation = totals.setdefault(
            source_row,
            {"qty": 0.0, "stock_qty": 0.0, "net_company": 0.0},
        )
        allocation["qty"] += returned_qty
        allocation["stock_qty"] += returned_stock_qty
        allocation["net_company"] += abs(flt(row.get("base_net_amount")))
    return totals


_RETURN_MONEY_TOLERANCE = 0.005
_RETURN_QTY_TOLERANCE = 0.0001


def _get_return_adjustment_remaining_balances(original_entry):
    adjustments = frappe.get_all(
        "Commission Entry",
        filters={
            "original_entry": original_entry,
            "event_type": "Return Adjustment",
            "docstatus": 1,
        },
        fields=[
            "name",
            "commission_basis_amount",
            "earned_commission",
            "accrued_amount",
            "source_qty",
        ],
        order_by="name desc",
    )
    if not adjustments:
        return []
    adjustment_names = [row.name for row in adjustments]
    reversals = frappe.get_all(
        "Commission Entry",
        filters={
            "reverses_entry": ["in", adjustment_names],
            "event_type": "Cancellation Reversal",
            "docstatus": 1,
        },
        fields=[
            "reverses_entry",
            "commission_basis_amount",
            "earned_commission",
            "accrued_amount",
            "source_qty",
        ],
    )
    reversed_totals = {}
    for reversal in reversals:
        totals = reversed_totals.setdefault(
            reversal.reverses_entry,
            {
                "basis": 0.0,
                "commission": 0.0,
                "accrued": 0.0,
                "qty": 0.0,
            },
        )
        totals["basis"] += abs(flt(reversal.commission_basis_amount))
        totals["commission"] += abs(flt(reversal.earned_commission))
        totals["accrued"] += abs(flt(reversal.accrued_amount))
        totals["qty"] += abs(flt(reversal.source_qty))

    results = []
    for adjustment in adjustments:
        reversed_amounts = reversed_totals.get(adjustment.name, {})
        remaining = {
            "name": adjustment.name,
            "basis": (
                abs(flt(adjustment.commission_basis_amount))
                - reversed_amounts.get("basis", 0.0)
            ),
            "commission": (
                abs(flt(adjustment.earned_commission))
                - reversed_amounts.get("commission", 0.0)
            ),
            "accrued": (
                abs(flt(adjustment.accrued_amount))
                - reversed_amounts.get("accrued", 0.0)
            ),
            "qty": (
                abs(flt(adjustment.source_qty))
                - reversed_amounts.get("qty", 0.0)
            ),
        }
        tolerances = {
            "basis": _RETURN_MONEY_TOLERANCE,
            "commission": _RETURN_MONEY_TOLERANCE,
            "accrued": _RETURN_MONEY_TOLERANCE,
            "qty": _RETURN_QTY_TOLERANCE,
        }
        for field, tolerance in tolerances.items():
            if remaining[field] < -tolerance:
                frappe.throw(
                    _(
                        "Return Adjustment {0} is over-reversed for {1}. "
                        "Check its cancellation history."
                    ).format(adjustment.name, field)
                )
            remaining[field] = max(0.0, remaining[field])
        results.append(remaining)
    return results


def _allocate_cancellation_excess(original_entry, excess, adjustment_balances):
    remaining = {
        "commission": max(0.0, flt(excess.get("commission"))),
        "basis": max(0.0, flt(excess.get("basis"))),
        "accrued": max(0.0, flt(excess.get("accrued"))),
        "qty": max(0.0, flt(excess.get("qty"))),
    }
    allocations = []
    for adjustment in adjustment_balances:
        allocation = {
            "adjustment": adjustment["name"],
            "commission": flt(
                min(remaining["commission"], adjustment["commission"]), 2
            ),
            "basis": flt(
                min(remaining["basis"], adjustment["basis"]), 2
            ),
            "accrued": flt(
                min(remaining["accrued"], adjustment["accrued"]), 2
            ),
            "qty": flt(
                min(remaining["qty"], adjustment["qty"]), 6
            ),
        }
        if (
            allocation["commission"] > 0
            or allocation["basis"] > 0
            or allocation["accrued"] > 0
            or allocation["qty"] > 0
        ):
            allocations.append(allocation)
        remaining["commission"] = max(
            0.0, remaining["commission"] - allocation["commission"]
        )
        remaining["basis"] = max(0.0, remaining["basis"] - allocation["basis"])
        remaining["accrued"] = max(
            0.0, remaining["accrued"] - allocation["accrued"]
        )
        remaining["qty"] = max(0.0, remaining["qty"] - allocation["qty"])

    if (
        remaining["commission"] > _RETURN_MONEY_TOLERANCE
        or remaining["basis"] > _RETURN_MONEY_TOLERANCE
        or remaining["accrued"] > _RETURN_MONEY_TOLERANCE
        or remaining["qty"] > _RETURN_QTY_TOLERANCE
    ):
        frappe.throw(
            _(
                "Cannot fully reconcile cancellation excess for entry {0}: "
                "unallocated commission={1}, basis={2}, accrued={3}, qty={4}."
            ).format(
                original_entry,
                remaining["commission"],
                remaining["basis"],
                remaining["accrued"],
                remaining["qty"],
            )
        )
    return allocations


def _sum_return_adjustment_balances(adjustment_balances):
    total = {
        "basis": 0.0,
        "commission": 0.0,
        "accrued": 0.0,
        "qty": 0.0,
    }
    for adj in adjustment_balances:
        total["basis"] += adj["basis"]
        total["commission"] += adj["commission"]
        total["accrued"] += adj["accrued"]
        total["qty"] += adj["qty"]
    return total



def _active_return_financial_totals(original_entry):
    """Return active cumulative return adjustment balances."""
    adjustment_balances = (
        _get_return_adjustment_remaining_balances(original_entry)
    )
    return _sum_return_adjustment_balances(adjustment_balances)


def _get_original_split_share(orig, snapshot):
    company_basis = abs(flt(orig.company_basis_amount))
    allocated_basis = abs(flt(orig.commission_basis_amount))
    if company_basis <= 0:
        frappe.throw(
            _("Cannot determine original company basis for {0}.").format(
                orig.name
            )
        )
    derived_share = allocated_basis / company_basis
    snapshot_share = flt((snapshot or {}).get("split_share"))
    if snapshot_share > 0:
        if not 0 < snapshot_share <= 1.0001:
            frappe.throw(
                _("Invalid contributor split for {0}.").format(orig.name)
            )
        if abs(snapshot_share - derived_share) > 0.001:
            frappe.throw(
                _("Contributor split snapshot mismatch for {0}.").format(
                    orig.name
                )
            )
        return snapshot_share
    if 0 < derived_share <= 1.0001:
        return derived_share
    frappe.throw(
        _("Invalid contributor split for {0}.").format(orig.name)
    )


def _validate_existing_cancellation_reversal(
    existing_doc,
    target_adjustment,
    original_entry,
    allocation,
):
    if (
        existing_doc.reverses_entry != target_adjustment
        or existing_doc.original_entry != original_entry
        or abs(
            flt(existing_doc.earned_commission)
            - flt(allocation["commission"], 2)
        ) > _RETURN_MONEY_TOLERANCE
        or abs(
            flt(existing_doc.commission_basis_amount)
            - flt(allocation["basis"], 2)
        ) > _RETURN_MONEY_TOLERANCE
        or abs(
            flt(existing_doc.accrued_amount)
            - flt(allocation["accrued"], 2)
        ) > _RETURN_MONEY_TOLERANCE
        or abs(
            flt(existing_doc.source_qty)
            - flt(allocation["qty"], 6)
        ) > _RETURN_QTY_TOLERANCE
    ):
        frappe.throw(
            _(
                "Existing cancellation reversal {0} does not match "
                "the expected allocation for {1}."
            ).format(existing_doc.name, target_adjustment)
        )


def _calculate_return_basis_company(orig, original_item, allocation, snapshot, original_qty):
    """Calculate returned basis in company currency after contributor split.

    Handles Net Total, Gross Profit, and Fixed Amount basis types.
    Applies the original contributor split to the returned basis.
    """
    split_share = _get_original_split_share(orig, snapshot)
    basis_type = orig.commission_basis
    if basis_type == "Net Total":
        full_returned_basis = flt(allocation["net_company"])
    elif basis_type == "Gross Profit":
        original_net = abs(flt(original_item.base_net_amount))
        original_profit = abs(flt(orig.company_basis_amount))
        original_cost = original_net - original_profit
        if original_cost < -0.01:
            frappe.throw(
                _("Invalid original cost snapshot for entry {0}.").format(
                    orig.name
                )
            )
        original_stock_qty = _get_row_stock_qty(original_item)
        if original_stock_qty <= 0:
            frappe.throw(
                _("Cannot determine original stock quantity for row {0}.").format(
                    original_item.name
                )
            )
        returned_cost = (
            max(0.0, original_cost)
            * allocation["stock_qty"]
            / original_stock_qty
        )
        full_returned_basis = allocation["net_company"] - returned_cost
    elif basis_type in ("Fixed Amount", "Fixed"):
        if original_qty <= 0:
            frappe.throw(
                _("Invalid original quantity for entry {0}.").format(
                    orig.name
                )
            )
        quantity_ratio = (
            flt(allocation["qty"]) / original_qty
        )
        # company_basis_amount is stored in company currency
        # and represents the full basis before contributor allocation.
        full_returned_basis = (
            abs(flt(orig.company_basis_amount)) * quantity_ratio
        )
    else:
        full_returned_basis = flt(allocation["net_company"])
    # Apply contributor split.
    returned_basis = full_returned_basis * split_share
    return flt(returned_basis, 6)

def _validate_commission_fx_snapshot(orig):
    import math

    orig_comm_curr = getattr(orig, "commission_currency", None)
    orig_comp_curr = getattr(orig, "company_currency", None)

    if not orig_comm_curr or not orig_comp_curr:
        frappe.throw(
            _("Missing commission or company currency for entry {0}.").format(
                orig.name
            )
        )

    orig_rate = flt(orig.commission_to_company_rate)

    if not math.isfinite(orig_rate) or orig_rate <= 0:
        frappe.throw(
            _("Invalid commission-to-company rate for entry {0}.").format(
                orig.name
            )
        )

    if orig_comm_curr == orig_comp_curr:
        if abs(orig_rate - 1.0) > 1e-9:
            frappe.throw(
                _("Commission and company currencies match but rate is not 1.0 "
                  "for entry {0}.").format(orig.name)
            )

    if orig_comm_curr != orig_comp_curr and orig_rate <= 0:
        frappe.throw(
            _("Invalid commission-to-company rate for foreign currency "
              "entry {0}.").format(orig.name)
        )


def handle_return_invoice(doc, method=None):
    """Handle credit notes / returns: create proportional clawback entries.

    Uses exact original invoice item row matching via sales_invoice_item.
    Fails closed on missing/invalid original invoice references.
    """
    original_name = getattr(doc, "return_against", None)
    if not original_name:
        return

    if _entries_exist(doc.name):
        return

    # Lock original invoice to serialize concurrent return processing.
    frappe.db.sql(
        """
        SELECT name
        FROM `tabSales Invoice`
        WHERE name = %s
        FOR UPDATE
        """,
        original_name,
    )

    # Fail closed: do not suppress original invoice lookup errors.
    original_doc = frappe.get_doc("Sales Invoice", original_name)
    if original_doc.docstatus != 1:
        frappe.throw(_("Original invoice must be submitted."))
    if original_doc.company != doc.company:
        frappe.throw(
            _("Return and original invoice must have the same company.")
        )
    if original_doc.customer != doc.customer:
        frappe.throw(
            _("Return and original invoice must have the same customer.")
        )

    original_items = {row.name: row for row in (original_doc.items or [])}

    # Build exact row allocations from return document.
    allocations = _build_return_allocations(doc, original_doc)

    # Get submitted return quantities (authoritative source).
    returned_totals = _get_submitted_return_quantities(original_doc)
    invoice_fully_returned = _is_original_invoice_fully_returned(
        original_doc, returned_totals
    )

    original_entries = frappe.get_all(
        "Commission Entry",
        filters={"sales_invoice": original_name, "docstatus": 1},
        fields=[
            "name",
            "item_code",
            "source_row",
            "source_qty",
            "source_uom",
            "earned_commission",
            "commission_basis_amount",
            "company_basis_amount",
            "fixed_amount_snapshot",
            "calculation_snapshot",
            "beneficiary_type",
            "sales_person",
            "sales_partner",
            "commission_basis",
            "plan",
            "transaction_currency",
            "company_currency",
            "commission_currency",
            "conversion_rate_snapshot",
            "commission_to_company_rate",
            "pool_code",
        ],
    )
    if not original_entries:
        return

    # Process Fixed Per Invoice entries first (invoice-level, not row-level).
    # These do not require an anchor row in the current return.
    for orig in original_entries:
        # Validate commission currency and conversion rate consistency.
        _validate_commission_fx_snapshot(orig)

        snapshot = frappe.parse_json(orig.calculation_snapshot or "{}") or {}
        calc_method = snapshot.get("calculation_method") or "Percentage"
        if calc_method != "Fixed Per Invoice":
            continue
        if not invoice_fully_returned:
            continue

        item = orig.item_code
        key = orig.get("name")
        original_item = original_items.get(orig.source_row)
        if not original_item:
            # For invoice-level, try to find any original item.
            if not original_items:
                frappe.throw(
                    _("Missing original invoice items for entry {0}.").format(orig.name)
                )
            original_item = list(original_items.values())[0]

        original_qty = abs(flt(orig.source_qty))
        if original_qty <= 0:
            original_qty = abs(flt(original_item.qty))
        if original_qty <= 0:
            frappe.throw(
                _("Cannot determine original quantity for entry {0}.").format(
                    orig.name
                )
            )

        history = _active_return_financial_totals(orig.name)
        already_returned_qty = history.get("qty", 0.0)
        # Fixed Per Invoice: reverse only once (full invoice).
        if history.get("commission", 0.0) > 0:
            # Already reversed; skip.
            continue

        ratio = 1.0

        clawback_commission = -1 * flt(orig.earned_commission) * ratio
        clawback_basis = -1 * flt(orig.commission_basis_amount) * ratio

        from sales_commission_management.sales_commission_management.doctype.commission_accrual_batch.commission_accrual_batch import _accrued_for_entry

        _orig_accrued = _accrued_for_entry(orig.name)
        clawback_accrued = -1 * flt(_orig_accrued) * ratio

        ev_key = _event_key(
            "Sales Invoice",
            doc.name,
            orig.get("name"),
            orig.get("plan"),
            orig.get("pool_code"),
            orig.beneficiary_type,
            orig.sales_person or orig.sales_partner,
            "Return Adjustment",
        )

        existing = frappe.db.get_value(
            "Commission Entry", {"event_key": ev_key, "docstatus": ["!=", 2]}, "name"
        )
        if existing:
            continue

        entry = frappe.get_doc(
            {
                "doctype": "Commission Entry",
                "event_key": ev_key,
                "event_type": "Return Adjustment",
                "company": doc.company,
                "posting_date": doc.posting_date,
                "source_doctype": "Sales Invoice",
                "source_name": doc.name,
                "original_invoice": original_name,
                "return_invoice": doc.name,
                "plan": orig.plan,
                "pool_code": orig.pool_code,
                "beneficiary_type": orig.beneficiary_type,
                "sales_person": orig.sales_person,
                "sales_partner": orig.sales_partner,
                "source_row": getattr(orig, "source_row", None),
                "source_qty": flt(original_qty, 6),
                "original_entry": orig.name,
                "item_code": orig.item_code,
                "commission_basis": orig.commission_basis,
                "commission_basis_amount": flt(clawback_basis, 2),
                "transaction_currency": orig.transaction_currency,
                "company_currency": orig.company_currency,
                "conversion_rate_snapshot": orig.conversion_rate_snapshot,
                "commission_currency": orig.get("commission_currency"),
                "commission_to_company_rate": flt(
                    orig.commission_to_company_rate
                ),
                "earned_delta": flt(clawback_commission, 6),
                "sales_invoice": doc.name,
                "exchange_rate": orig.conversion_rate_snapshot,
                "earned_commission": flt(clawback_commission, 2),
                "collection_eligible_amount": flt(clawback_commission, 2),
                "payable_amount": flt(clawback_commission, 2),
                "accrued_amount": flt(clawback_accrued, 2),
                "paid_amount": 0,
                "settled_amount": 0,
                "outstanding_amount": flt(clawback_commission, 2),
                "status": "Reversed",
                "remarks": f"Clawback for return {doc.name} against {original_name}",
            }
        )
        try:
            entry.insert(ignore_permissions=True)
        except frappe.DuplicateEntryError:
            continue
        entry.submit()

    for orig in original_entries:
        item = orig.item_code
        key = orig.get("name")

        # Validate commission currency and conversion rate consistency.
        _validate_commission_fx_snapshot(orig)

        snapshot = frappe.parse_json(orig.calculation_snapshot or "{}") or {}
        calc_method = snapshot.get("calculation_method") or "Percentage"

        # Skip Fixed Per Invoice (handled above).
        if calc_method == "Fixed Per Invoice":
            continue

        original_item = original_items.get(orig.source_row)
        if not original_item:
            frappe.throw(
                _("Missing original invoice row for entry {0}.").format(orig.name)
            )

        original_qty = abs(flt(orig.source_qty))
        if original_qty <= 0:
            original_qty = abs(flt(original_item.qty))
        if original_qty <= 0:
            frappe.throw(
                _("Cannot determine original quantity for entry {0}.").format(
                    orig.name
                )
            )

        # Find the returned quantity for this exact original row.
        allocation = allocations.get(orig.source_row)
        if not allocation:
            # No return for this row; skip.
            continue

        rq = allocation["qty"]

        # Skip if no return quantity for this item.
        if rq <= 0:
            continue

        # Get active return financial totals (cancellation-aware).
        history = _active_return_financial_totals(orig.name)
        original_basis = abs(flt(orig.commission_basis_amount))
        original_commission = abs(flt(orig.earned_commission))
        original_accrued = abs(flt(history.get("accrued", 0))) or abs(flt(orig.accrued_amount))

        # remaining quantity that can still be returned
        already_returned_qty = history.get("qty", 0.0)
        remaining_qty = max(0.0, original_qty - already_returned_qty)
        if remaining_qty <= 0:
            continue

        # Reject if return exceeds remaining (do not silently cap).
        if rq > remaining_qty + 0.0001:
            frappe.throw(
                _(
                    "Return quantity {0} exceeds remaining returnable quantity {1} "
                    "for item {2} in entry {3}."
                ).format(rq, remaining_qty, item, orig.name)
            )

        effective_returned = min(rq, remaining_qty)
        if effective_returned <= 0:
            continue

        # Cumulative returned quantity (for Fixed Per Line).
        cumulative_returned = already_returned_qty + effective_returned

        # Calculate the reversal ratio.
        basis_type = orig.commission_basis

        if calc_method in ("Fixed Per Unit",):
            ratio = (effective_returned / original_qty) if original_qty else 0.0
        elif calc_method == "Fixed Per Line":
            # This contains all submitted returns, including the current return.
            cumulative_returned = flt(
                returned_totals.get(orig.source_row, 0)
            )
            if cumulative_returned > original_qty + 0.0001:
                frappe.throw(
                    _("Returned quantity exceeds original quantity for row {0}.").format(
                        orig.source_row
                    )
                )
            # Wait until the complete original invoice row is returned.
            if cumulative_returned < original_qty - 0.0001:
                continue
            # Reverse only the remaining commission.
            remaining_commission = max(
                0.0,
                original_commission - history.get("commission", 0.0)
            )
            if remaining_commission <= 0.005:
                continue
            ratio = remaining_commission / original_commission
        else:
            returned_basis = _calculate_return_basis_company(
                orig, original_item, allocation, snapshot, original_qty
            )
            if original_basis > 0:
                ratio = min(1.0, returned_basis / original_basis)
            else:
                ratio = (effective_returned / original_qty) if original_qty else 0.0

        # Enforce cumulative limits.
        remaining_basis_limit = max(0.0, original_basis - history.get("basis", 0.0))
        if clawback_basis_abs := abs(flt(orig.commission_basis_amount) * ratio):
            if clawback_basis_abs > remaining_basis_limit + 0.0001:
                ratio = remaining_basis_limit / original_basis if original_basis > 0 else 0.0
        remaining_comm_limit = max(0.0, original_commission - history.get("commission", 0.0))
        clawback_comm_abs = abs(flt(orig.earned_commission) * ratio)
        if clawback_comm_abs > remaining_comm_limit + 0.0001:
            ratio = remaining_comm_limit / original_commission if original_commission > 0 else 0.0

        if ratio < 0:
            ratio = 0.0
        if ratio > 1:
            ratio = 1.0

        if ratio <= 0:
            continue

        clawback_commission = -1 * flt(orig.earned_commission) * ratio
        clawback_basis = -1 * flt(orig.commission_basis_amount) * ratio

        # Reverse the accrued portion proportionally.
        from sales_commission_management.sales_commission_management.doctype.commission_accrual_batch.commission_accrual_batch import _accrued_for_entry

        _orig_accrued = _accrued_for_entry(orig.name)
        _already_reversed_accrued = history.get("accrued", 0.0)
        _remaining_accrued = max(0.0, abs(_orig_accrued) - _already_reversed_accrued)
        clawback_accrued = -1 * min(abs(_orig_accrued) * ratio, _remaining_accrued)

        ev_key = _event_key(
            "Sales Invoice",
            doc.name,
            orig.get("name"),
            orig.get("plan"),
            orig.get("pool_code"),
            orig.beneficiary_type,
            orig.sales_person or orig.sales_partner,
            "Return Adjustment",
        )

        existing = frappe.db.get_value(
            "Commission Entry", {"event_key": ev_key, "docstatus": ["!=", 2]}, "name"
        )
        if existing:
            continue
        ev_key = _event_key(
            "Sales Invoice",
            doc.name,
            orig.get("name"),
            orig.get("plan"),
            orig.get("pool_code"),
            orig.beneficiary_type,
            orig.sales_person or orig.sales_partner,
            "Return Adjustment",
        )

        existing = frappe.db.get_value(
            "Commission Entry", {"event_key": ev_key, "docstatus": ["!=", 2]}, "name"
        )
        if existing:
            continue

        entry = frappe.get_doc(
            {
                "doctype": "Commission Entry",
                "event_key": ev_key,
                "event_type": "Return Adjustment",
                "company": doc.company,
                "posting_date": doc.posting_date,
                "source_doctype": "Sales Invoice",
                "source_name": doc.name,
                "original_invoice": original_name,
                "return_invoice": doc.name,
                "plan": orig.plan,
                "pool_code": orig.pool_code,
                "beneficiary_type": orig.beneficiary_type,
                "sales_person": orig.sales_person,
                "sales_partner": orig.sales_partner,
                "source_row": getattr(orig, "source_row", None),
                "source_qty": flt(effective_returned, 6),
                "original_entry": orig.name,
                "item_code": orig.item_code,
                "commission_basis": orig.commission_basis,
                "commission_basis_amount": flt(clawback_basis, 2),
                "transaction_currency": orig.transaction_currency,
                "company_currency": orig.company_currency,
                "conversion_rate_snapshot": orig.conversion_rate_snapshot,
                "commission_currency": orig.get("commission_currency"),
                "commission_to_company_rate": flt(
                    orig.commission_to_company_rate
                ),
                "earned_delta": flt(clawback_commission, 6),
                "sales_invoice": doc.name,
                "exchange_rate": orig.conversion_rate_snapshot,
                "earned_commission": flt(clawback_commission, 2),
                "collection_eligible_amount": flt(clawback_commission, 2),
                "payable_amount": flt(clawback_commission, 2),
                "accrued_amount": flt(clawback_accrued, 2),
                "paid_amount": 0,
                "settled_amount": 0,
                "outstanding_amount": flt(clawback_commission, 2),
                "status": "Reversed" if ratio >= 0.999 else "Adjusted",
                "remarks": f"Clawback for return {doc.name} against {original_name}",
            }
        )
        try:
            entry.insert(ignore_permissions=True)
        except frappe.DuplicateEntryError:
            continue
        entry.submit()
def reverse_commission_entries(doc, method=None):
    """Triggered on Sales Invoice cancellation: full clawback of all entries."""
    existing_entries = frappe.get_all(
        "Commission Entry",
        filters={"sales_invoice": doc.name, "docstatus": 1},
        fields=[
            "name",
            "earned_commission",
            "commission_basis_amount",
            "plan",
            "pool_code",
            "beneficiary_type",
            "sales_person",
            "sales_partner",
            "item_code",
            "source_row",
        ],
    )
    for entry in existing_entries:
        original = frappe.get_doc("Commission Entry", entry.name)
        from sales_commission_management.sales_commission_management.doctype.commission_accrual_batch.commission_accrual_batch import _accrued_for_entry

        # Fail closed: do not fall back to stale accrued_amount.
        _total_accrued = _accrued_for_entry(original.name, use_company_amount=True)
        # Subtract amounts already reclassified to payable by submitted settlements.
        from sales_commission_management.sales_commission_management.doctype.commission_settlement.commission_settlement import _reclassified_for_entry

        _already_reclassified = _reclassified_for_entry(original.name, use_company_amount=True)
        _orig_accrued = flt(_total_accrued - _already_reclassified)
        ev_key = _event_key(
            "Sales Invoice",
            doc.name,
            entry.get("source_row"),
            entry.get("plan"),
            entry.get("pool_code"),
            entry.beneficiary_type,
            entry.sales_person or entry.sales_partner,
            "Cancellation Reversal",
        )
        existing = frappe.db.get_value(
            "Commission Entry", {"event_key": ev_key, "docstatus": ["!=", 2]}, "name"
        )
        if existing:
            continue

        clawback = frappe.copy_doc(original)
        clawback.event_key = ev_key
        clawback.event_type = "Cancellation Reversal"
        clawback.earned_commission = -1 * flt(original.earned_commission)
        clawback.commission_basis_amount = -1 * flt(original.commission_basis_amount)
        clawback.earned_delta = -1 * flt(original.earned_delta)
        # Reverse all financial balances, not just earned.
        clawback.collection_eligible_amount = -1 * flt(original.collection_eligible_amount)
        clawback.payable_amount = -1 * flt(original.payable_amount)
        clawback.payable_eligible_delta = -1 * flt(original.payable_eligible_delta)
        clawback.reserved_delta = -1 * flt(original.reserved_delta)
        clawback.paid_delta = -1 * flt(original.paid_delta)
        clawback.settled_amount = -1 * flt(original.settled_amount)
        clawback.paid_amount = -1 * flt(original.paid_amount)
        clawback.accrued_amount = -1 * flt(original.accrued_amount)
        clawback.outstanding_amount = clawback.earned_commission
        clawback.status = "Reversed"
        clawback.collection_status = "Not Eligible"
        clawback.settlement_status = "Not Reserved"
        clawback.remarks = f"Clawback due to cancellation of {doc.name}"
        clawback.reverses_entry = original.name
        clawback.original_entry = original.original_entry or original.name
        try:
            clawback.insert(ignore_permissions=True)
        except frappe.DuplicateEntryError:
            continue
        clawback.submit()

        # Mark the original entry as reversed so it is excluded from settlement
        # and its balances are zeroed (prevents double-availability). This is a
        # critical financial operation; failure must abort the cancellation.
        frappe.db.set_value(
            "Commission Entry",
            original.name,
            {
                "status": "Reversed",
                "collection_eligible_amount": 0,
                "payable_amount": 0,
                "accrued_amount": 0,
                "outstanding_amount": 0,
                "collection_status": "Not Eligible",
                "settlement_status": "Not Reserved",
            },
            update_modified=False,
        )

        # Reverse the accrual GL if an accrual was posted for this entry.
        if _orig_accrued and abs(_orig_accrued) > 0.005:
            if not frappe.db.exists("Commission Settings"):
                frappe.throw(
                    _("Commission Settings must be configured before invoice cancellation.")
                )
            settings = frappe.get_single("Commission Settings")
            accounting_mode = getattr(settings, "accounting_mode", None)
            if accounting_mode not in ("Subledger Until Settlement", "Periodic Accrual"):
                frappe.throw(_("Invalid Commission Accounting Mode."))
            if accounting_mode == "Periodic Accrual":
                accrued_account = None
                expense_account = None
                cost_center = None
                if settings:
                    for row in (getattr(settings, "account_mappings", None) or []):
                        if getattr(row, "company", None) == original.company and getattr(row, "enabled", 1):
                            accrued_account = getattr(row, "accrued_commission_payable_account", None)
                            expense_account = getattr(row, "commission_expense_account", None)
                            cost_center = getattr(row, "cost_center", None)
                            break
                    if not accrued_account:
                        accrued_account = getattr(settings, "default_accrued_liability_account", None)
                    if not expense_account:
                        expense_account = getattr(settings, "default_commission_expense_account", None)
                    if not cost_center:
                        cost_center = getattr(settings, "default_cost_center", None)
                if not accrued_account or not expense_account:
                    frappe.throw(
                        _("Accrued liability or expense account not configured for company {0}.").format(original.company)
                    )
                amt = abs(_orig_accrued)
                je = frappe.get_doc(
                        {
                            "doctype": "Journal Entry",
                            "voucher_type": "Journal Entry",
                            "company": original.company,
                            "posting_date": original.posting_date,
                            "accounts": [
                                {
                                    "account": accrued_account,
                                    "debit_in_account_currency": flt(amt, _cp(original.company_currency)),
                                },
                                {
                                    "account": expense_account,
                                    "credit_in_account_currency": flt(amt, _cp(original.company_currency)),
                                },
                            ],
                            "user_remark": "Reverse accrued commission for cancelled invoice {0}".format(
                                doc.name
                            ),
                        }
                    )
                if cost_center:
                    for a in je.accounts:
                        a.cost_center = cost_center
                je.insert(ignore_permissions=True)
                je.submit()


def reconcile_return_cancellation(doc, method=None):
    """Reconcile commission clawbacks when a return invoice is cancelled.

    A cancellation of an earlier partial return can leave later returns having
    created more clawback than is currently justified. This function recomputes
    the required cumulative clawback per original commission entry from the
    remaining submitted returns and creates immutable compensating entries for
    any excess.
    """
    if not getattr(doc, "is_return", 0):
        return
    original_name = getattr(doc, "return_against", None)
    if not original_name:
        return

    # Lock original invoice to serialize concurrent processing.
    frappe.db.sql(
        "SELECT name FROM `tabSales Invoice` WHERE name = %s FOR UPDATE",
        original_name,
    )
    original_doc = frappe.get_doc("Sales Invoice", original_name)
    if original_doc.docstatus != 1:
        return

    returned_totals = _get_submitted_return_quantities(original_doc)
    cumulative_allocations = _get_submitted_return_allocations_cumulative(original_doc)
    invoice_fully_returned = _is_original_invoice_fully_returned(
        original_doc, returned_totals
    )

    original_items = {row.name: row for row in (original_doc.items or [])}

    original_entries = frappe.get_all(
        "Commission Entry",
        filters={"sales_invoice": original_name, "docstatus": 1},
        fields=[
            "name",
            "item_code",
            "source_row",
            "source_qty",
            "earned_commission",
            "commission_basis_amount",
            "company_basis_amount",
            "calculation_snapshot",
            "beneficiary_type",
            "sales_person",
            "sales_partner",
            "commission_basis",
            "plan",
            "pool_code",
            "original_entry",
            "accrued_amount",
        ],
    )
    if not original_entries:
        return

    for orig in original_entries:
        orig_name = orig.name
        snapshot = frappe.parse_json(orig.calculation_snapshot or "{}") or {}
        calc_method = snapshot.get("calculation_method") or "Percentage"

        original_item = original_items.get(orig.source_row)
        original_qty = abs(flt(orig.source_qty))
        if original_qty <= 0 and original_item is not None:
            original_qty = abs(flt(original_item.qty))
        if original_qty <= 0:
            continue

        original_basis = abs(flt(orig.commission_basis_amount))
        original_commission = abs(flt(orig.earned_commission))
        from sales_commission_management.sales_commission_management.doctype.commission_accrual_batch.commission_accrual_batch import _accrued_for_entry

        original_accrued = abs(flt(_accrued_for_entry(orig_name)))

        # Compute required cumulative ratio.
        ratio = 0.0
        if calc_method == "Fixed Per Invoice":
            ratio = 1.0 if invoice_fully_returned else 0.0
        elif calc_method == "Fixed Per Line":
            cum_qty = returned_totals.get(orig.source_row, 0.0)
            ratio = 1.0 if (cum_qty >= original_qty - 0.0001 and original_qty > 0) else 0.0
        elif calc_method in ("Fixed Per Unit", "Fixed Amount", "Fixed"):
            cum_qty = returned_totals.get(orig.source_row, 0.0)
            ratio = min(1.0, cum_qty / original_qty) if original_qty else 0.0
        else:
            cum_alloc = cumulative_allocations.get(orig.source_row)
            if cum_alloc and original_item is not None:
                returned_basis = _calculate_return_basis_company(
                    orig, original_item, cum_alloc, snapshot, original_qty
                )
                if original_basis > 0:
                    ratio = min(1.0, returned_basis / original_basis)
                else:
                    cum_qty = returned_totals.get(orig.source_row, 0.0)
                    ratio = min(1.0, cum_qty / original_qty) if original_qty else 0.0

        required_commission = original_commission * ratio
        required_basis = original_basis * ratio
        required_accrued = original_accrued * ratio
        required_qty = original_qty * ratio

        existing = _active_return_financial_totals(orig_name)
        excess_commission = existing.get("commission", 0.0) - required_commission
        excess_basis = existing.get("basis", 0.0) - required_basis
        excess_accrued = existing.get("accrued", 0.0) - required_accrued
        excess_qty = existing.get("qty", 0.0) - required_qty

        if (
            excess_commission <= 0.005
            and excess_basis <= 0.005
            and excess_accrued <= 0.005
            and excess_qty <= _RETURN_QTY_TOLERANCE
        ):
            continue

        # Get per-adjustment remaining balances for bounded allocation.
        adjustment_balances = _get_return_adjustment_remaining_balances(
            orig_name
        )
        if not adjustment_balances:
            continue

        # Allocate the excess across adjustments.
        excess = {
            "commission": excess_commission,
            "basis": excess_basis,
            "accrued": excess_accrued,
            "qty": excess_qty,
        }
        allocations = _allocate_cancellation_excess(
            orig_name, excess, adjustment_balances
        )

        # Create a compensating entry for each allocation.
        for alloc in allocations:
            target_adjustment = alloc["adjustment"]
            adj_doc = frappe.get_doc(
                "Commission Entry", target_adjustment
            )

            alloc_ev_key = _event_key(
                "Sales Invoice",
                doc.name,
                orig_name,
                orig.plan,
                orig.pool_code,
                orig.beneficiary_type,
                orig.sales_person or orig.sales_partner,
                "Cancellation Reversal",
            )
            # Make event key unique per adjustment.
            alloc_ev_key = alloc_ev_key + "|" + target_adjustment

            existing_entry = frappe.db.get_value(
                "Commission Entry",
                {"event_key": alloc_ev_key, "docstatus": ["!=", 2]},
                "name",
            )
            if existing_entry:
                existing_doc = frappe.get_doc(
                    "Commission Entry", existing_entry
                )
                _validate_existing_cancellation_reversal(
                    existing_doc,
                    target_adjustment,
                    orig_name,
                    alloc,
                )
                continue

            comp = frappe.copy_doc(adj_doc)
            comp.event_key = alloc_ev_key
            comp.event_type = "Cancellation Reversal"
            comp.source_doctype = "Sales Invoice"
            comp.source_name = doc.name
            comp.sales_invoice = doc.name
            comp.return_invoice = doc.name
            comp.posting_date = doc.posting_date
            comp.earned_commission = flt(alloc["commission"], 2)
            comp.commission_basis_amount = flt(alloc["basis"], 2)
            comp.earned_delta = flt(alloc["commission"], 6)
            comp.accrued_amount = flt(alloc["accrued"], 2)
            comp.collection_eligible_amount = flt(alloc["commission"], 2)
            comp.payable_amount = flt(alloc["commission"], 2)
            comp.outstanding_amount = flt(alloc["commission"], 2)
            comp.source_qty = flt(alloc["qty"], 6)
            comp.status = "Reversed"
            comp.collection_status = "Not Eligible"
            comp.settlement_status = "Not Reserved"
            comp.remarks = (
                "Reconciliation clawback reversal due to cancellation of "
                + doc.name
            )
            comp.reverses_entry = target_adjustment
            comp.original_entry = orig_name
            try:
                comp.insert(ignore_permissions=True)
            except frappe.DuplicateEntryError:
                dup_name = frappe.db.get_value(
                    "Commission Entry",
                    {"event_key": alloc_ev_key, "docstatus": ["!=", 2]},
                    "name",
                )
                if not dup_name:
                    raise
                dup_doc = frappe.get_doc(
                    "Commission Entry", dup_name
                )
                _validate_existing_cancellation_reversal(
                    dup_doc,
                    target_adjustment,
                    orig_name,
                    alloc,
                )
                continue
            comp.submit()

def _is_lock_contention_error(exc):
    """Check if an exception is a MariaDB lock contention error (1205).

    Frappe v15/v16 wraps PyMySQL exceptions in frappe.QueryTimeoutError,
    so we need to unwrap the exception chain to find the original error code.
    """
    pending = [exc]
    seen = set()
    while pending:
        current = pending.pop()
        if not isinstance(current, BaseException) or id(current) in seen:
            continue
        seen.add(id(current))
        args = getattr(current, "args", ()) or ()
        if args and args[0] in (1205, "1205"):
            return True
        if args and isinstance(args[0], BaseException):
            pending.append(args[0])
        for attr in ("orig", "__cause__", "__context__"):
            nested = getattr(current, attr, None)
            if isinstance(nested, BaseException):
                pending.append(nested)
    return False


def lock_referenced_invoices(payment_entry, method=None):
    """Acquire sorted invoice locks before the Payment Entry is saved.

    Called from before_submit and before_cancel hooks to establish a
    consistent lock order (Sales Invoice -> Payment Entry -> ...) and
    prevent deadlocks during concurrent payment processing.
    """
    references = payment_entry.get("references") or []
    invoices = sorted(
        {
            ref.reference_name
            for ref in references
            if ref.reference_doctype == "Sales Invoice" and ref.reference_name
        }
    )
    for invoice in invoices:
        frappe.db.sql(
            "SELECT name FROM `tabSales Invoice` WHERE name = %s FOR UPDATE",
            (invoice,),
        )


def update_commission_payment_status(payment_entry, method=None):
    """On Payment Entry submit/cancel, recompute collection eligibility.

    Customer Payment Entry allocations unlock collection_eligible_amount
    (commission unlocked by customer collection), NOT paid_amount (actual
    payout to the beneficiary). This keeps customer-collection eligibility
    separate from salesperson/supplier payout.

    Eligibility is recalculated from ALL submitted Payment Entries for the
    invoice, so multiple payments, partial payments, and cancellations are
    handled correctly and idempotently.
    """
    if getattr(payment_entry, "payment_type", None) != "Receive":
        return
    if getattr(payment_entry, "party_type", None) != "Customer":
        return
    if frappe.db.exists("Commission Settings"):
        settings = frappe.get_single("Commission Settings")
        if not getattr(settings, "enabled", 1):
            return
        if not getattr(settings, "enable_payment_entry", 1):
            return
    references = payment_entry.get("references") or []
    invoices = set()
    for ref in references:
        if ref.reference_doctype == "Sales Invoice" and ref.reference_name:
            invoices.add(ref.reference_name)

    for invoice in sorted(invoices):
        # Read invoice total via locking read after acquiring the invoice lock.
        locked_inv = frappe.db.sql(
            "SELECT base_grand_total FROM `tabSales Invoice` WHERE name = %s FOR UPDATE",
            (invoice,),
            as_dict=True,
        )
        inv_total = flt(locked_inv[0].base_grand_total) if locked_inv else 0.0
        invoice_total = abs(inv_total)
        if invoice_total <= 0:
            frappe.throw(_("Invalid invoice total for {0}.").format(invoice))
        # Compute cumulative collection ratio from all submitted Payment Entries
        # referencing this invoice, converted to invoice transaction currency.
        cumulative_allocated = 0.0
        # Lock only Payment Entry Reference rows (not Payment Entry parent rows)
        # to avoid deadlocks with concurrent Payment Entry submissions.
        # Read PE details separately without locking PE rows.
        locked_refs = frappe.db.sql(
            """
            SELECT
                allocated_amount,
                parent
            FROM `tabPayment Entry Reference`
            WHERE
                reference_doctype = %s
                AND reference_name = %s
            FOR UPDATE
            """,
            ("Sales Invoice", invoice),
            as_dict=True,
        )
        # Cache PE details using a locking read to get current committed data.
        pe_cache = {}
        for r in locked_refs or []:
            pe_name = r.parent
            if pe_name not in pe_cache:
                # Use NOWAIT to fail fast instead of waiting (deadlock prevention).
                # Retry a few times before giving up.
                for attempt in range(3):
                    try:
                        locked_pe = frappe.db.sql(
                            """
                            SELECT
                                docstatus,
                                payment_type,
                                party_type,
                                source_exchange_rate
                            FROM `tabPayment Entry`
                            WHERE name = %s
                            FOR UPDATE NOWAIT
                            """,
                            (pe_name,),
                            as_dict=True,
                        )
                        pe_cache[pe_name] = locked_pe[0] if locked_pe else None
                        break
                    except Exception as e:
                        # Unwrap Frappe's QueryTimeoutError to find MariaDB error 1205.
                        if not _is_lock_contention_error(e):
                            raise
                        if attempt < 2:
                            import time
                            time.sleep(0.1)
                            continue
                        frappe.throw(
                            _("Could not obtain lock on Payment Entry {0}. Please retry.").format(pe_name)
                        )
            pe = pe_cache[pe_name]
            if not pe or pe.docstatus != 1:
                continue
            if pe.payment_type not in (None, "Receive"):
                continue
            if pe.party_type not in (None, "Customer"):
                continue
            allocated_raw = flt(r.allocated_amount)
            if allocated_raw == 0:
                continue
            # allocated_raw is denominated in the party (receivable) account
            # currency; convert to company currency using the source exchange
            # rate (paid-from -> company), NOT the target (paid-to -> company)
            # rate, which would mis-scale foreign-currency collections.
            source_rate = flt(pe.source_exchange_rate)
            if source_rate <= 0:
                frappe.throw(
                    _("Invalid source exchange rate in Payment Entry {0}.").format(
                        pe_name
                    )
                )
            allocated = allocated_raw * source_rate
            cumulative_allocated += abs(allocated)

        cumulative_ratio = min(max(cumulative_allocated / invoice_total, 0.0), 1.0)

        entries = frappe.get_all(
            "Commission Entry",
            filters={
                "sales_invoice": invoice,
                "docstatus": 1,
            },
            fields=[
                "name",
                "earned_commission",
                "collection_eligible_amount",
                "settled_amount",
                "paid_amount",
                "status",
                "accrual_status",
                "collection_status",
                "settlement_status",
                "commission_currency",
                "company_currency",
            ],
        )
        for e in entries:
            # Lock the entry row and retrieve current values in one locking read.
            locked = frappe.db.sql(
                """
                SELECT
                    name,
                    earned_commission,
                    collection_eligible_amount,
                    commission_currency,
                    company_currency
                FROM `tabCommission Entry`
                WHERE name = %s
                FOR UPDATE
                """,
                (e.name,),
                as_dict=True,
            )
            if not locked:
                continue
            le = locked[0]
            e.earned_commission = flt(le.earned_commission)
            e.collection_eligible_amount = flt(le.collection_eligible_amount)
            e.commission_currency = le.commission_currency
            e.company_currency = le.company_currency
            earned = flt(e.earned_commission)
            # Resolve payout_basis from the entry's plan.
            payout_basis = "On Collection"
            plan_name = frappe.get_cached_value("Commission Entry", e.name, "plan")
            if plan_name:
                payout_basis = (
                    frappe.get_cached_value(
                        "Commission Plan", plan_name, "payout_basis"
                    )
                    or "On Collection"
                )
            if payout_basis == "Accrued":
                # Commission is eligible immediately upon earning (no payment gate).
                target_eligible = earned
            else:
                target_eligible = earned * cumulative_ratio
            # clamp
            if target_eligible < 0:
                target_eligible = 0.0
            if target_eligible > earned:
                target_eligible = earned
            settled = flt(e.settled_amount or e.paid_amount)
            outstanding = target_eligible - settled
            if outstanding < 0:
                outstanding = 0.0
            if target_eligible <= 0:
                coll_status = "Not Eligible"
            elif target_eligible < earned:
                coll_status = "Partially Eligible"
            else:
                coll_status = "Eligible"
            if settled > 0:
                new_status = "Partially Paid" if target_eligible > settled else "Paid"
            elif target_eligible > 0:
                new_status = "Payable"
            else:
                new_status = "Earned"
            frappe.db.set_value(
                "Commission Entry",
                e.name,
                {
                    "collection_eligible_amount": flt(target_eligible, 6),
                    "payable_amount": flt(target_eligible, 6),
                    "payable_eligible_delta": flt(
                        target_eligible - flt(e.collection_eligible_amount), 6
                    ),
                    "outstanding_amount": flt(outstanding, 6),
                    "status": new_status,
                    "collection_status": coll_status,
                },
                update_modified=False,
            )

            # Determine if this is a cancellation/reversal.
            is_cancel = getattr(payment_entry, "docstatus", 0) == 2

            # Find ALL matching reference rows for this invoice from the current
            # payment entry (not a stale ref from an earlier loop).
            matching_refs = [
                r for r in (payment_entry.get("references") or [])
                if r.reference_doctype == "Sales Invoice" and r.reference_name == invoice
            ]

            # The eligibility delta for THIS payment event.
            total_add_eligible = target_eligible - flt(e.collection_eligible_amount)

            # Distribute the eligibility delta proportionally across reference rows.
            total_raw = sum(
                flt(getattr(r, "allocated_amount", 0)) if r else 0.0
                for r in (matching_refs or [])
            )
            if not total_raw:
                total_raw = 1.0

            remaining_delta = flt(total_add_eligible, 6)

            # Record one immutable collection event per reference row.
            for idx, matching_ref in enumerate(matching_refs or [None]):
                ref_allocated_raw = flt(getattr(matching_ref, "allocated_amount", 0)) if matching_ref else 0.0
                # Fail closed: do not fall back to raw amount on conversion failure.
                ref_allocated = payment_entry.get_allocated_amount_in_transaction_currency(
                    ref_allocated_raw, "Sales Invoice", invoice
                )

                # Proportional share of the eligibility delta.
                if total_raw:
                    share = ref_allocated_raw / total_raw
                else:
                    share = 1.0
                if idx == len(matching_refs or [None]) - 1:
                    ref_delta = flt(remaining_delta, 6)
                else:
                    ref_delta = flt(total_add_eligible * share, 6)
                    remaining_delta = flt(remaining_delta - ref_delta, 6)

                event_key = "|".join([
                    e.name or "",
                    payment_entry.name or "",
                    getattr(matching_ref, "name", None) or "",
                    "Collection" if not is_cancel else "Reversal",
                ])

                # Idempotency: skip if event already exists.
                existing = frappe.db.get_value(
                    "Commission Collection Event",
                    {"event_key": event_key, "parent": e.name},
                    "name",
                )
                if existing:
                    continue

                source_rate = flt(getattr(payment_entry, "source_exchange_rate", 0))
                if source_rate <= 0:
                    frappe.throw("Invalid source exchange rate for Payment Entry {0}".format(payment_entry.name))

                frappe.get_doc({
                    "doctype": "Commission Collection Event",
                    "event_key": event_key,
                    "parent": e.name,
                    "parentfield": "collection_events",
                    "parenttype": "Commission Entry",
                    "payment_entry": payment_entry.name,
                    "payment_reference": getattr(matching_ref, "name", None),
                    "posting_date": getattr(payment_entry, "posting_date", None),
                    "party_account_currency": getattr(payment_entry, "party_account_currency", None),
                    "invoice_currency": frappe.get_value("Sales Invoice", invoice, "currency"),
                    "commission_currency": getattr(e, "commission_currency", None),
                    "company_currency": getattr(e, "company_currency", None),
                    "allocated_amount": flt(ref_allocated_raw, 6),
                    "invoice_currency_allocated": flt(ref_allocated, 6),
                    "company_currency_allocated": flt(ref_allocated_raw * source_rate, 6),
                    "collection_ratio": flt(cumulative_ratio * 100.0, 6),
                    "eligibility_delta": flt(ref_delta, 6),
                    "is_reversal": 1 if is_cancel else 0,
                }).insert(ignore_permissions=True)

def recompute_period_slabs():
    """Scheduled daily job: recompute period attainment per beneficiary and metric.

    Aggregates Commission Entry earned amounts within each open Commission Period's
    date range and updates the matching Commission Target attainment. Entries are
    never mutated; only target attainment (a cached, rebuildable value) is written.
    """
    try:
        periods = frappe.get_all(
            "Commission Period",
            filters={"status": ["in", ["Open", "Reopened"]]},
            fields=["name", "period_from", "period_to", "company"],
        )
    except Exception:
        frappe.log_error(
            frappe.get_traceback(), "recompute_period_slabs: period lookup failed"
        )
        return

    for period in periods:
        try:
            _recompute_one_period(period)
        except Exception:
            frappe.log_error(
                frappe.get_traceback(),
                "recompute_period_slabs failed for period {}".format(
                    period.get("name")
                ),
            )


def _sales_person_ancestors(name):
    """Return list of ancestor (and self) Sales Person names via parent link."""
    result = []
    current = name
    seen = set()
    while current and current not in seen:
        seen.add(current)
        result.append(current)
        try:
            parent = frappe.get_cached_value(
                "Sales Person", current, "parent_sales_person"
            )
        except Exception:
            parent = None
        if not parent or parent == current:
            break
        current = parent
    return result


def _period_range(posting_date, period_type):
    """Return (from_date, to_date) for the period containing posting_date."""
    from datetime import date, timedelta

    d = getdate(posting_date)
    pt = (period_type or "Monthly").strip()
    if pt == "Quarterly":
        q = (d.month - 1) // 3
        start = date(d.year, q * 3 + 1, 1)
        end_month = q * 3 + 3
        if end_month == 12:
            end = date(d.year, 12, 31)
        else:
            end = date(d.year, end_month + 1, 1) - timedelta(days=1)
        return start, end
    if pt == "Yearly":
        return date(d.year, 1, 1), date(d.year, 12, 31)
    if pt == "Per Invoice":
        return d, d
    # Monthly (default)
    start = date(d.year, d.month, 1)
    if d.month == 12:
        end = date(d.year, 12, 31)
    else:
        end = date(d.year, d.month + 1, 1) - timedelta(days=1)
    return start, end


_METRIC_TO_BASIS = {
    "Net Sales": "Net Total",
    "Gross Profit": "Gross Profit",
    "Collected Net Sales": "Net Total",
}


def _plan_target_value(plan):
    """Sum target values from plan's targets child table."""
    total = 0.0
    for t in plan.get("targets") or []:
        total += flt(getattr(t, "target_value", 0))
    return flt(total)


def _plan_cumulative_metric(plan, company, posting_date):
    """Aggregate target metric from existing (submitted) Commission Entries for
    this plan within the period containing posting_date. Dedupes by source so
    multi-contributor invoices are not double-counted."""
    if not plan or not posting_date:
        return 0.0
    period_type = "Monthly"
    for _t in getattr(plan, "targets", None) or []:
        _pt = getattr(_t, "period_type", None)
        if _pt:
            period_type = _pt
            break
    pf, pt = _period_range(posting_date, period_type)
    target_metric = getattr(plan, "target_metric", None)
    basis_name = _METRIC_TO_BASIS.get(target_metric)

    filters = {
        "docstatus": 1,
        "plan": plan.name,
        "posting_date": ["between", [pf, pt]],
    }
    if company:
        filters["company"] = company

    try:
        entries = frappe.get_all(
            "Commission Entry",
            filters=filters,
            fields=[
                "commission_basis",
                "company_basis_amount",
                "source_name",
                "source_row",
                "sales_invoice",
            ],
        )
    except Exception:
        return 0.0

    # Dedupe by source invoice+row; keep one company_basis_amount per source.
    seen = set()
    total = 0.0
    for e in entries or []:
        if basis_name and e.get("commission_basis") != basis_name:
            continue
        key = (e.get("sales_invoice") or e.get("source_name"), e.get("source_row"))
        if key in seen:
            continue
        seen.add(key)
        total += flt(e.get("company_basis_amount"))
    return flt(total)


def _recompute_one_period(period):
    period_from = period.get("period_from")
    period_to = period.get("period_to")
    company = period.get("company")
    if not period_from or not period_to:
        return

    filters = {
        "docstatus": 1,
        "posting_date": ["between", [period_from, period_to]],
    }
    if company:
        filters["company"] = company

    entries = frappe.get_all(
        "Commission Entry",
        filters=filters,
        fields=[
            "beneficiary_type",
            "sales_person",
            "sales_partner",
            "commission_basis",
            "company_basis_amount",
            "source_name",
            "source_row",
            "sales_invoice",
            "company",
            "plan",
        ],
    )

    # Aggregate target metric per (beneficiary_type, beneficiary), deduping by
    # source invoice+row so multi-contributor invoices are not double-counted.
    attainment = {}
    seen_by_ben = {}
    for e in entries or []:
        bt = e.get("beneficiary_type")
        ben = e.get("sales_person") if bt == "Sales Person" else e.get("sales_partner")
        if not ben:
            continue
        basis_name = e.get("commission_basis")
        metric_key = (bt, ben)
        seen = seen_by_ben.setdefault(metric_key, set())
        src_key = (e.get("sales_invoice") or e.get("source_name"), e.get("source_row"))
        if src_key in seen:
            continue
        seen.add(src_key)
        attainment.setdefault(metric_key, 0.0)
        attainment[metric_key] += flt(e.get("company_basis_amount"))

    # Update matching Commission Targets. For group (Sales Person container)
    # targets, aggregate across descendants.
    # First, collect all targets in this period's company.
    target_filters = {"docstatus": ["!=", 2]}
    if company:
        try:
            plan_names = (
                frappe.get_all(
                    "Commission Plan",
                    filters={"company": company, "docstatus": ["!=", 2]},
                    pluck="name",
                )
                or []
            )
        except Exception:
            plan_names = []
        if plan_names:
            target_filters["parent"] = ["in", plan_names]
    try:
        all_targets = frappe.get_all(
            "Commission Target",
            filters=target_filters,
            fields=[
                "name",
                "beneficiary_type",
                "sales_person",
                "sales_partner",
                "target_value",
                "target_metric",
                "parent",
            ],
        )
    except Exception:
        all_targets = []

    # Build a map from beneficiary to its descendants (for group expansion).
    descendant_cache = {}

    def _descendants(name):
        if name in descendant_cache:
            return descendant_cache[name]
        try:
            is_group = frappe.get_cached_value("Sales Person", name, "is_group")
        except Exception:
            is_group = 0
        if not is_group:
            res = [name]
        else:
            try:
                lft, rgt = frappe.get_cached_value("Sales Person", name, ["lft", "rgt"])
            except Exception:
                lft, rgt = None, None
            if not lft or not rgt:
                res = [name]
            else:
                try:
                    res = frappe.get_all(
                        "Sales Person",
                        filters={"lft": [">=", lft], "rgt": ["<=", rgt]},
                        pluck="name",
                    ) or [name]
                except Exception:
                    res = [name]
        descendant_cache[name] = res
        return res

    for t in all_targets or []:
        bt = t.get("beneficiary_type")
        ben = t.get("sales_person") if bt == "Sales Person" else t.get("sales_partner")
        if not ben:
            continue
        target_metric = t.get("target_metric")
        target_basis = _METRIC_TO_BASIS.get(target_metric)
        target_value = flt(t.get("target_value"))
        if target_value <= 0:
            continue
        # Determine the set of beneficiaries to aggregate.
        if bt == "Sales Person":
            members = _descendants(ben)
        else:
            members = [ben]
        total_metric = 0.0
        seen2 = set()
        for e in entries or []:
            ebt = e.get("beneficiary_type")
            eben = (
                e.get("sales_person")
                if ebt == "Sales Person"
                else e.get("sales_partner")
            )
            if ebt != bt or eben not in members:
                continue
            if target_basis and e.get("commission_basis") != target_basis:
                continue
            # Scope by parent plan so a target is only credited entries
            # generated under its own Commission Plan.
            if t.get("parent") and e.get("plan") and e.get("plan") != t.get("parent"):
                continue
            src_key = (
                e.get("sales_invoice") or e.get("source_name"),
                e.get("source_row"),
            )
            if src_key in seen2:
                continue
            seen2.add(src_key)
            total_metric += flt(e.get("company_basis_amount"))

        pct = (total_metric / target_value) * 100.0
        try:
            frappe.db.set_value(
                "Commission Target",
                t.get("name"),
                {"attainment_percentage": flt(pct, 2)},
                update_modified=False,
            )
        except Exception:
            pass
