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
                "sales_invoice",
                "posting_date",
                "company",
                "sales_person",
                "sales_partner",
                "commission_plan",
                "commission_rule",
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
    status,
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

    commission_currency = company_currency
    if getattr(plan, "commission_currency_policy", None) == "Transaction Currency":
        commission_currency = txn_currency
    elif getattr(plan, "commission_currency_policy", None) == "Fixed Currency":
        commission_currency = (
            getattr(plan, "commission_currency", None) or company_currency
        )

    commission_rate = flt(getattr(rule, "rate_percentage", 0))
    fixed_amount = flt(getattr(rule, "fixed_amount", 0))

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
            "paid_amount": 0,
            "outstanding_amount": flt(item_commission, 2),
            "status": status,
            "remarks": "",
            "calculation_snapshot": frappe.as_json(
                {
                    "plan": plan.name,
                    "rule": getattr(rule, "name", None),
                    "basis_name": basis_name,
                    "basis_company": basis_company,
                    "basis_txn": basis_txn,
                    "split_share": split_share,
                    "item_commission": item_commission,
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


def generate_commission_entries(doc, method=None):
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

    slabs = plan.get("slabs") or []
    slab_method = getattr(plan, "slab_calculation_method", None) or "Progressive"

    fixed_invoice_rules_applied = set()

    for item in doc.items or []:
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
            item_commission = _calculate_slab_amount(basis_company, slabs, slab_method)
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

        for rep in contributors:
            split_share = (flt(rep.get("allocated_percentage", 0)) or 0) / 100.0
            if split_share <= 0:
                continue
            ctype = rep.get("contributor_type") or "Sales Person"
            pool_code = rep.get("pool_code") or "INTERNAL_SALES"

            if ctype == "Sales Partner":
                partner = rep.get("sales_partner")
                rate = flt(getattr(rule, "rate_percentage", 0))
                if not partner or not rate:
                    continue
                rep_commission = item_commission * split_share
                if rep_commission <= 0:
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
                    "Accrued",
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
                "Accrued",
            )

        # External sales partner from invoice. Skip if the same partner is already
        # a contributor in the plan, to avoid double-counting.
        plan_partner_beneficiaries = {
            getattr(pc, "sales_partner", None)
            for pc in (plan.get("contributors") or [])
            if getattr(pc, "contributor_type", "") == "Sales Partner"
        }
        if external_partner and external_partner not in plan_partner_beneficiaries:
            # Plan rule rate is the single commission-rate source; do not use the
            # native Sales Partner rate from the invoice.
            partner_commission = basis_company * (
                flt(getattr(rule, "rate_percentage", 0)) / 100.0
            )
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
                    "Accrued",
                )


def _calculate_slab_amount(basis_amount, slabs, method="Progressive"):
    if not slabs:
        return 0.0
    slabs = sorted(
        slabs,
        key=lambda s: flt(
            s.min_attainment
            if hasattr(s, "min_attainment")
            else getattr(s, "from_amount", 0)
        ),
    )

    # support both old (from_amount) and new (min_attainment) field names
    def _from(s):
        return flt(getattr(s, "min_attainment", None) or getattr(s, "from_amount", 0))

    def _to(s):
        v = getattr(s, "max_attainment", None)
        if v is None:
            v = getattr(s, "to_amount", None)
        return flt(v) if v else float("inf")

    def _rate(s):
        return flt(getattr(s, "rate_percentage", 0))

    def _fixed(s):
        return flt(getattr(s, "fixed_bonus", None) or getattr(s, "fixed_amount", 0))

    slabs = sorted(slabs, key=_from)

    if method == "Single":
        applicable = None
        for slab in slabs:
            if _from(slab) <= basis_amount <= _to(slab):
                applicable = slab
                break
        if applicable is None and slabs:
            applicable = slabs[-1]
        if not applicable:
            return 0.0
        rate = _rate(applicable) / 100.0
        fixed = _fixed(applicable)
        return flt(basis_amount * rate + fixed, 2)

    total = 0.0
    for slab in slabs:
        band_top = min(basis_amount, _to(slab))
        band_amount = max(0.0, band_top - _from(slab))
        if band_amount <= 0:
            continue
        rate = _rate(slab) / 100.0
        total += band_amount * rate
    if slabs and _fixed(slabs[0]):
        total += _fixed(slabs[0])
    return flt(total, 2)


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
        fields=["item_code", "source_qty"],
    )
    for pr in prior_returns:
        already_returned[pr.item_code] = already_returned.get(pr.item_code, 0) + abs(
            flt(pr.source_qty)
        )

    for orig in original_entries:
        oq = original_qty.get(orig.item_code, 0) or abs(flt(orig.source_qty)) or 1
        rq = returned_qty.get(orig.item_code, 0)
        # remaining quantity that can still be returned
        remaining = max(0.0, oq - already_returned.get(orig.item_code, 0))
        effective_returned = min(rq, remaining) if remaining > 0 else 0
        ratio = (effective_returned / oq) if oq else 0.0
        if ratio < 0:
            ratio = 0.0
        if ratio > 1:
            ratio = 1.0

        clawback_commission = -1 * flt(orig.earned_commission) * ratio
        clawback_basis = -1 * flt(orig.commission_basis_amount) * ratio

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
                "item_code": orig.item_code,
                "commission_basis": orig.commission_basis,
                "commission_basis_amount": flt(clawback_basis, 2),
                "transaction_currency": orig.transaction_currency,
                "company_currency": orig.company_currency,
                "conversion_rate_snapshot": orig.conversion_rate_snapshot,
                "commission_currency": orig.company_currency,
                "earned_delta": flt(clawback_commission, 6),
                "sales_invoice": doc.name,
                "exchange_rate": orig.conversion_rate_snapshot,
                "earned_commission": flt(clawback_commission, 2),
                "payable_amount": 0,
                "paid_amount": 0,
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
        clawback.outstanding_amount = clawback.earned_commission
        clawback.status = "Reversed"
        clawback.remarks = f"Clawback due to cancellation of {doc.name}"
        try:
            clawback.insert(ignore_permissions=True)
        except frappe.DuplicateEntryError:
            continue
        clawback.submit()


def update_commission_payment_status(payment_entry, method=None):
    """On Payment Entry submit/cancel, allocate paid amounts to commission entries proportionally."""
    is_cancel = getattr(payment_entry, "docstatus", 0) == 2
    references = payment_entry.get("references") or []
    for ref in references:
        if ref.reference_doctype != "Sales Invoice":
            continue
        invoice = ref.reference_name
        allocated = flt(ref.allocated_amount)
        if allocated == 0:
            continue
        if is_cancel:
            allocated = -abs(allocated)

        try:
            inv = frappe.get_doc("Sales Invoice", invoice)
        except Exception:
            continue
        invoice_total = flt(inv.grand_total) or 1.0
        proportion = allocated / invoice_total

        entries = frappe.get_all(
            "Commission Entry",
            filters={
                "sales_invoice": invoice,
                "docstatus": 1,
                "status": ["in", ["Accrued", "Payable", "Partially Paid"]],
            },
            fields=["name", "earned_commission", "paid_amount"],
        )
        for e in entries:
            add_paid = flt(e.earned_commission) * proportion
            new_paid = flt(e.paid_amount) + add_paid
            outstanding = flt(e.earned_commission) - new_paid
            if outstanding <= 0.005:
                new_status = "Paid"
                outstanding = 0
            elif new_paid > 0:
                new_status = "Partially Paid"
            else:
                new_status = "Payable"
            frappe.db.set_value(
                "Commission Entry",
                e.name,
                {
                    "paid_amount": flt(new_paid, 6),
                    "outstanding_amount": flt(outstanding, 6),
                    "status": new_status,
                    "payable_amount": flt(e.earned_commission, 6),
                    "paid_delta": flt(add_paid, 6),
                },
                update_modified=False,
            )


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
            "earned_commission",
            "company",
        ],
    )

    # Aggregate earned amounts per (beneficiary_type, beneficiary).
    attainment = {}
    for e in entries:
        bt = e.get("beneficiary_type")
        ben = e.get("sales_person") if bt == "Sales Person" else e.get("sales_partner")
        if not ben:
            continue
        key = (bt, ben)
        attainment.setdefault(key, 0.0)
        attainment[key] += flt(e.get("earned_commission"))

    # Update matching Commission Targets (attainment is cached/rebuildable).
    for (bt, ben), earned in attainment.items():
        try:
            targets = frappe.get_all(
                "Commission Target",
                filters={
                    "beneficiary_type": bt,
                    "sales_person" if bt == "Sales Person" else "sales_partner": ben,
                    "docstatus": ["!=", 2],
                },
                fields=["name", "target_value", "target_metric"],
            )
        except Exception:
            targets = []
        for t in targets:
            target_value = flt(t.get("target_value"))
            if target_value <= 0:
                continue
            pct = (earned / target_value) * 100.0
            try:
                frappe.db.set_value(
                    "Commission Target",
                    t.get("name"),
                    {"attainment_percentage": flt(pct, 2)},
                    update_modified=False,
                )
            except Exception:
                # Field may not exist on this schema version; ignore.
                pass
