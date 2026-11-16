# Change Log

This file records changes to the Sales Commission Management for ERPNext app so coders can track what changed in the codebase. Entries are grouped by module.

## commission_entry (doctype/commission_entry/commission_entry.py)

- Added `_resolve_posted_cost(item, company, company_currency)` to resolve actual posted cost (COGS) from the Stock Ledger Entry for the invoice (or its Delivery Note) when computing Gross Profit basis. Falls back to `valuation_rate * qty` and logs `Commission Gross Profit Cost Source` when no traceable source exists.
- Added `_item_group_path(item_group)` and deepest-path matching in `_match_rule`: Item > deepest Item Group ancestor > All Items, with equal-priority ambiguity checks.
- `_resolve_plan(customer, customer_group, territory, company, posting_date)` now validates plan date ranges and scope precedence (Specific Customer > Customer Group > Territory > All), then priority.
- Idempotency via `event_key` on `Commission Entry`; `_insert_entry` returns the existing name when a duplicate `event_key` is present.
- `_calculate_basis` supports `Net Total`, `Gross Profit` (posted cost), `Fixed Amount` (and `Fixed` alias), negative-basis and zero-basis handling, and `_apply_floor_cap` min/max caps.
- `_calculate_slab_amount` supports both `from_amount`/`to_amount` and `min_attainment`/`max_attainment` field names, `Progressive` and `Single` methods.
- `generate_commission_entries` prefers plan `contributors` over invoice `sales_team`; applies `grant_commission`, `include_free_items`, calculation methods (`Fixed Per Unit`, `Fixed Per Line`, `Fixed Per Invoice`, slabs, percentage), and external Sales Partner entries.
- Added `_native_overlap_guard` to neutralize native ERPNext commission fields when `block_native_hrms_overlap` is enabled.
- `handle_return_invoice` creates proportional clawback entries referencing the original invoice/entry.
- `reverse_commission_entries` creates full negative (append-only) clawback entries on cancellation.
- `update_commission_payment_status` allocates paid amounts proportionally and updates `paid_amount`, `outstanding_amount`, `status` (Payable / Partially Paid / Paid).
- Submitted `Commission Entry` records are immutable except payment-tracking fields (enforced in `validate`).
- `recompute_period_slabs` now aggregates Commission Entry earned amounts per beneficiary within each open Commission Period and updates matching Commission Target attainment percentages. Entries are never mutated.

## commission_settlement (doctype/commission_settlement/commission_settlement.py)

- Added `_clear_accrued_liability()` on submit: posts a Journal Entry moving accrued commission liability to commission payable when accounts are configured; skips with a logged error otherwise.
- Settlement creation: Additional Salary (internal) or Purchase Invoice (external), with cancellation reversal.
- Totals computed in `_compute_totals` (earned, payable, reserved, paid, outstanding).

## commission_settings (doctype/commission_settings/commission_settings.json)

- Single DocType with engine ownership, accounting mode, native-overlap guard, rounding policy, and per-company account mappings (`Commission Account Mapping`).

## api (api.py)

- `get_commission_summary`: whitelisted GET with optional `sales_person`, `from_date`, `to_date`, `status`, `company` filters.
- `get_commission_totals`: whitelisted GET returning `earned`, `paid`, `outstanding`, `count`.

## Documentation

- Updated `docs/calculation.md` to match current implementation: posted-cost Gross Profit, plan contributors, native rate not substituted, immutability, native-overlap guard, commission currency policy, slab field-name aliases.
- `docs/api.md`, `docs/hooks.md`, `docs/architecture.md` verified against code.

## Implementation plan fixes (commission_entry.py, hooks.py, commission_settlement.py, api.py)

- Added `neutralize_native_commission` (before_submit) for in-memory native commission neutralization; registered in hooks.py.
- `_native_overlap_guard` no longer writes to DB (just logs); mutation handled by before_submit.
- `handle_return_invoice`: cumulative return tracking so multiple partial returns never exceed original commission.
- `CommissionSettlement._clear_accrued_liability`: now throws (hard failure) when accounts are not configured instead of skipping.
- API permission scoping via `_enforce_sales_person_scope`.

## Code-review fixes (commission_entry.py, api.py)

- `_calculate_basis`: defined missing `company_currency` (was NameError on Gross Profit path).
- `handle_return_invoice`: query now selects `source_qty` instead of non-existent `qty`; ratio uses `source_qty`; removed duplicate `return_invoice` key.
- External Sales Partner commission now uses `rule.rate_percentage` instead of native invoice rate.
- `_native_overlap_guard`: guard `if not row.name: continue` before `frappe.db.set_value`.
- `api.py`: added `_enforce_sales_person_scope` so non-privileged users can only query their own linked Sales Person.

## ERPNext-source conflict fixes (commission_entry.py, hooks.py)

- `_resolve_posted_cost(item, company, company_currency)`: filters cancelled SLEs and only outgoing movements (`actual_qty < 0`); prefers Delivery Note over Sales Invoice to avoid double-counting.
- `_calculate_basis` (Gross Profit): converts COGS into transaction currency before subtracting from `net_txn` (was mixing company/txn currencies).
- `generate_commission_entries`: removed convoluted walrus-based `grant_commission` guard; made "Fixed Per Invoice" de-dup per-rule via a tracking set instead of fragile `item is doc.items[0]` identity check.
- External Sales Partner path skipped when that partner is already a plan contributor (prevents double-counting).
- Added `neutralize_native_commission_on_update` (registered as `before_update_after_submit` on Sales Invoice) to mutate the in-memory submitted invoice so native ERPNext rates are not recalculated when Sales Team is edited post-submit.
- `handle_return_invoice`: removed duplicate `return_invoice` key (was incorrectly set to `original_name`; now `doc.name`).
- `validate`: submitted entries immutable except payment-tracking fields (`paid_amount`, `outstanding_amount`, `status`, `payable_amount`, `paid_delta`); payment allocation uses `frappe.db.set_value`.
- `_native_overlap_guard`: neutralizes native ERPNext commission fields (`commission_rate`, `incentives`, `total_commission`) when `block_native_hrms_overlap` is enabled.
- API reads switched from `frappe.get_all` to `frappe.get_list` for permission enforcement.

## hooks.py

- Added `before_update_after_submit` registration for Sales Invoice → `neutralize_native_commission_on_update`.

## Notes for coders

- Hook registration lives in `hooks.py` (`doc_events`, `scheduler_events`); run `bench migrate` + restart after changes.
- Commission is stored in company currency; transaction currency and exchange rate are persisted for audit.
- Reversal is append-only; never delete generated `Commission Entry` records.
