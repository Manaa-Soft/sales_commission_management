import frappe
from frappe import _
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
                "commission_plan",
                "commission_rule",
                "pool_code",
                "beneficiary_type",
                "event_type",
                "event_key",
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


def generate_commission_entries(doc, method=None, force=False):
    """Triggered on Sales Invoice / POS Invoice on_submit. Creates Commission Entry records."""
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


def handle_return_invoice(doc, method=None):
    """Handle credit notes / returns: create proportional clawback entries."""
    original_name = getattr(doc, "return_against", None)
    if not original_name:
        return

    if _entries_exist(doc.name):
        return

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
            "beneficiary_type",
            "sales_person",
            "sales_partner",
            "commission_basis",
            "plan",
            "transaction_currency",
            "company_currency",
            "commission_currency",
            "conversion_rate_snapshot",
            "pool_code",
        ],
    )
    if not original_entries:
        return

    # Use absolute quantities for returns (ERPNext stores return qty as negative).
    returned_qty = {}
    for item in doc.items or []:
        returned_qty[item.item_code] = returned_qty.get(item.item_code, 0) + abs(
            flt(item.qty)
        )

    try:
        original_doc = frappe.get_doc("Sales Invoice", original_name)
    except Exception:
        original_doc = None
    original_qty = {}
    if original_doc:
        for item in original_doc.items:
            original_qty[item.item_code] = original_qty.get(item.item_code, 0) + abs(
                flt(item.qty)
            )

    # Cumulative already-returned quantity from prior Return Adjustment entries,
    # so multiple partial returns never reverse more than the original commission.
    already_returned = {}
    prior_returns = frappe.get_all(
        "Commission Entry",
        filters={
            "original_invoice": original_name,
            "event_type": "Return Adjustment",
            "docstatus": 1,
        },
        fields=["item_code", "source_row", "original_entry", "source_qty"],
    )
    for pr in prior_returns:
        key = pr.get("original_entry") or pr.get("source_row") or pr.get("item_code")
        already_returned[key] = already_returned.get(key, 0) + abs(
            flt(pr.source_qty)
        )

    # Detect ambiguous return allocation: ambiguity arises only when the same
    # item_code appears on multiple *distinct* original invoice item rows.
    # Multiple contributors sharing one invoice row are not ambiguous.
    rows_by_item = {}
    for oe in original_entries:
        rows_by_item.setdefault(oe.item_code, set()).add(
            getattr(oe, "source_row", None)
        )

    for orig in original_entries:
        item = orig.item_code
        distinct_rows = rows_by_item.get(item, set())
        if len(distinct_rows) > 1 and returned_qty.get(item, 0) > 0:
            frappe.throw(
                _(
                    "Return allocation is ambiguous: item {0} appears on multiple "
                    "distinct original invoice rows. Match returns to the original "
                    "invoice item row explicitly."
                ).format(item)
            )

        key = orig.get("name")
        oq = abs(flt(orig.source_qty)) or original_qty.get(item, 0) or 1
        rq = returned_qty.get(item, 0)
        # remaining quantity that can still be returned
        remaining = max(0.0, oq - already_returned.get(key, 0))
        effective_returned = min(rq, remaining) if remaining > 0 else 0
        ratio = (effective_returned / oq) if oq else 0.0
        if ratio < 0:
            ratio = 0.0
        if ratio > 1:
            ratio = 1.0

        clawback_commission = -1 * flt(orig.earned_commission) * ratio
        clawback_basis = -1 * flt(orig.commission_basis_amount) * ratio

        # Reverse the accrued portion of the original entry proportionally so
        # Periodic Accrual settlement netting reflects the return.
        from sales_commission_management.sales_commission_management.doctype.commission_accrual_batch.commission_accrual_batch import _accrued_for_entry

        # Fail closed: do not fall back to stale accrued_amount.
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
                "source_qty": flt(effective_returned, 6),
                "original_entry": orig.name,
                "item_code": orig.item_code,
                "commission_basis": orig.commission_basis,
                "commission_basis_amount": flt(clawback_basis, 2),
                "transaction_currency": orig.transaction_currency,
                "company_currency": orig.company_currency,
                "conversion_rate_snapshot": orig.conversion_rate_snapshot,
                "commission_currency": orig.get("commission_currency") or orig.get("company_currency"),
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
                "original_entry": orig.name,
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
        _total_accrued = _accrued_for_entry(original.name)
        # Subtract amounts already reclassified to payable by submitted settlements.
        from sales_commission_management.sales_commission_management.doctype.commission_settlement.commission_settlement import _reclassified_for_entry

        _already_reclassified = _reclassified_for_entry(original.name)
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
                if accrued_account and expense_account:
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
                                    "debit_in_account_currency": flt(amt, 2),
                                },
                                {
                                    "account": expense_account,
                                    "credit_in_account_currency": flt(amt, 2),
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
    references = payment_entry.get("references") or []
    invoices = set()
    for ref in references:
        if ref.reference_doctype == "Sales Invoice" and ref.reference_name:
            invoices.add(ref.reference_name)

    for invoice in invoices:
        inv = frappe.get_doc("Sales Invoice", invoice)
        invoice_total = abs(flt(inv.base_grand_total))
        if invoice_total <= 0:
            frappe.throw(_("Invalid invoice total for {0}.").format(invoice))

        # Lock the invoice row to serialize concurrent payment processing.
        frappe.db.sql(
            "SELECT name FROM `tabSales Invoice` WHERE name = %s FOR UPDATE",
            (invoice,),
        )

        # Compute cumulative collection ratio from all submitted Payment Entries
        # referencing this invoice, converted to invoice transaction currency.
        cumulative_allocated = 0.0
        # Locking read: retrieve allocation data via FOR UPDATE so we see
        # the latest committed data under REPEATABLE READ isolation.
        locked_refs = frappe.db.sql(
            """
            SELECT
                per.allocated_amount,
                per.parent,
                pe.docstatus,
                pe.payment_type,
                pe.party_type,
                pe.source_exchange_rate
            FROM `tabPayment Entry Reference` per
            INNER JOIN `tabPayment Entry` pe
                ON pe.name = per.parent
            WHERE
                per.reference_doctype = %s
                AND per.reference_name = %s
            FOR UPDATE
            """,
            ("Sales Invoice", invoice),
            as_dict=True,
        )
        for r in locked_refs or []:
            if r.docstatus != 1:
                continue
            if r.payment_type not in (None, "Receive"):
                continue
            if r.party_type not in (None, "Customer"):
                continue
            allocated_raw = flt(r.allocated_amount)
            if allocated_raw == 0:
                continue
            # allocated_raw is denominated in the party (receivable) account
            # currency; convert to company currency using the source exchange
            # rate (paid-from -> company), NOT the target (paid-to -> company)
            # rate, which would mis-scale foreign-currency collections.
            source_rate = flt(r.source_exchange_rate)
            if source_rate <= 0:
                frappe.throw(
                    _("Invalid source exchange rate in Payment Entry {0}.").format(
                        r.parent
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
