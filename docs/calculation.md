# Commission Calculation

## Trigger

`generate_commission_entries(doc, method)` is invoked on `Sales Invoice.on_submit` (and `POS Invoice.on_submit`). Credit notes (`doc.is_return`) route to `handle_return_invoice`.

## Steps

1. **Idempotency** — if `Commission Entry` records already exist for the invoice (not cancelled), skip.
2. **Returns** — if `doc.is_return` is set, route to `handle_return_invoice` (proportional clawback).
3. **Resolve plan** — `_resolve_plan(customer, customer_group, territory, company, posting_date)`:
   - Enabled plans for the company whose `valid_from`/`valid_to` bracket the posting date.
   - Scope match: `Specific Customer` > `Customer Group` > `Territory` > `All`, then higher `priority`.
   - None → no commission.
4. **Contributors** — prefer the plan's `contributors` (split %, pool code, type); otherwise fall back to `doc.sales_team` (Sales Persons). An external `doc.sales_partner` with `commission_rate` creates an additional `EXTERNAL_PARTNER` entry.
5. **Per item**:
   - Skip if `item.grant_commission == 0` (when rule `exclude_if_grant_commission_zero` is set).
   - Skip free items (`rate == 0` and `net_amount == 0`) unless rule `include_free_items` is set.
   - `match_commission_rule(plan, item_code, item_group)` (priority-aware: Item → deepest Item Group → All Items; ambiguous equal-priority ties raise).
   - Skip if no rule.
   - **Basis amount** (company currency):
     - `Net Total` → `item.base_net_amount` (fallback `item.net_amount`)
     - `Gross Profit` → `base_net_amount - posted_cost`, where posted cost is resolved from the Stock Ledger Entry for the invoice/Delivery Note (fallback `valuation_rate * qty`, logged as `Commission Gross Profit Cost Source` when no traceable source exists)
     - `Fixed Amount` → `item.qty * rule.fixed_amount`
   - Skip if `basis_amount <= 0` (unless `allow_negative_basis`).
   - **Commission amount** (company currency):
     - `Fixed Per Unit` → `fixed_amount * qty`
     - `Fixed Per Line` → `fixed_amount`
     - `Fixed Per Invoice` → `fixed_amount` on the first item only
     - Slabs (if `use_slabs`) → progressive or single slab per plan's `slab_calculation_method`
     - Otherwise → `basis * rate%` (+ optional `fixed_amount * qty`)
   - Apply `min_commission` / `max_commission` caps.
6. **Per contributor**:
   - Internal (Sales Person): `split_share = allocated_percentage / 100`; commission = `item_commission * split_share`. The plan rule rate is the single commission-rate source; the native Sales Person master rate is not substituted.
   - Sales Partner (plan contributor): `item_commission * split_share`.
   - External (invoice Sales Partner): `basis * commission_rate%`.
   - Insert + submit a `Commission Entry` (status `Accrued`) with multi-currency fields and an idempotency `event_key`.

## Reversal (clawback)

- `on_cancel`: `reverse_commission_entries` creates full negative entries (append-only).
- Credit note / return: `handle_return_invoice` creates proportional negative/adjusted entries referencing the original invoice and original entry.

## Payment allocation

`Payment Entry.on_submit`/`on_cancel` → `update_commission_payment_status` allocates paid amounts to commission entries proportionally and updates `paid_amount`, `outstanding_amount`, `status` (Payable / Partially Paid / Paid).

## Slabs

`Commission Slab` rows on the plan. Applied when a rule has `use_slabs` checked. Supports `Progressive` (per-band) and `Single` (whole amount at reached tier). Field names supported: `from_amount`/`to_amount` and `min_attainment`/`max_attainment`.

## Native-overlap guard

When `block_native_hrms_overlap` is enabled, native ERPNext commission fields on the invoice's Sales Team (`commission_rate`, `incentives`) and `total_commission` are neutralized (set to 0) to prevent double counting.

## Multi-currency

Commission is always stored in company currency (`commission_basis_amount`, `earned_commission`). Transaction currency, company currency and exchange rate are persisted for audit. Plan `commission_currency_policy` (Company / Transaction / Fixed) selects the entry's `commission_currency`.

## Immutability

Submitted `Commission Entry` records are immutable except for payment-tracking fields (`paid_amount`, `outstanding_amount`, `status`, `payable_amount`, `paid_delta`), which are written via `frappe.db.set_value`.
