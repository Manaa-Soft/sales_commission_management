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
- Added `_is_sales_person_group(name)` helper. `generate_commission_entries` now skips contributors whose `sales_person` is a group (non-earning container) and logs a warning; no commission entry is created with a group name as beneficiary. Existing group-named entries are not migrated. Report filters continue to expand groups to descendants via `_expand_sales_person`.
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

## report (report/<name>/)

- Added 20 Script Reports (type `Script Report`) under `report/<name>/` with `<name>.py` (`execute(filters)` returning columns/data), `<name>.json` metadata (filters, roles, type), and optional `<name>.js` client formatter.
- Reports: Commission Register, Commission Accrual Register, Commission by Item, Commission by Item Group, Commission by Plan, Commission Expense Analysis, Commission GL Reconciliation, Commission Payable Aging, Commission ROI by Item Group, Collection-Based Commission, Gross Profit vs Commission, Payment Allocation Register, Returns & Clawbacks, Sales Partner Commission Statement, Sales Person Commission Statement, Sales Person Performance, Settlement Register, Settlement Variance Report, Slab Achievement, Target Achievement.
- Run via the Frappe Report list or the Workspace shortcuts; smoke-test with `frappe.desk.query_report.run`.

## workspace (workspace/Sales Commission Management/)

- Added the `Workspace` DocType "Sales Commission Management" (`standard = 1`, icon `percent`, module `Sales Commission Management`) providing sidebar navigation (Plans & Rules, Commission Ledger, Settlements, Periods & Targets, Settings) and shortcuts to 18 of the 20 Script Reports (Slab Achievement and Target Achievement are reachable via the Report list).
- Synced to the site by `bench migrate`; re-run `bench migrate` (then restart bench) after report/workspace changes. `bench build` rebuilds desk assets.

## Naming series (DocType autoname)

- `Commission Plan`: `autoname = "field:plan_name"` (user-entered name).
- `Commission Entry`: `autoname = "naming_series:"`, series `CE-.YYYY.-.####`.
- `Commission Settlement`: `autoname = "naming_series:"`, series `CS-.YYYY.-.####`.
- `Commission Accrual Batch`: `autoname = "naming_series:"`, series `CAB-.YYYY.-.####`.
- `Commission Period`: `autoname = "naming_series:"`, series `CP-.YYYY.-.####`.
- `Commission Target`: `autoname = "naming_series:"`, series `CT-.YYYY.-.####`.
- Added `naming_series` Select field (hidden, default first option) to the above DocTypes.
- Child tables and `Commission Settings` (Single) unchanged.

## Data patch: rename_hashed_to_series

- Added `patches/rename_hashed_to_series.py` and registered it in `patches.txt`.
- Renames pre-existing hash-named documents (e.g. `dm05c2un98`) to representative series names (`CE-2026-00001`, `CS-2026-00001`, `CAB-2026-00001`, `CP-2026-00001`, `CT-2026-00001`).
- Runs during `bench migrate`; idempotent (skips already-series-named records).
- Commission Plan uses `field:plan_name` (no rename needed).

## Balance model & accrual reclassification (commission_entry, commission_settlement, commission_accrual_batch)

- Added `collection_eligible_amount` and `settled_amount` fields to Commission Entry.
- `update_commission_payment_status`: customer Payment Entry allocations now update `collection_eligible_amount` (collection eligibility), not `paid_amount` (actual payout). `paid_amount` is reserved for actual salesperson/supplier payouts.
- `_available_balance_for_entry`: available = collection-eligible - reserved - actually settled; removed the earned-as-payable fallback.
- `_validate_entries`: row-level locking (`SELECT ... FOR UPDATE`), beneficiary/company/currency/date consistency validation, explicit zero handling, defaults only on blank.
- `_clear_accrued_liability`: reclassifies only `amount_included` (capped at unreclassified accrued balance, converted to company currency); accrued amount falls back to earned commission when unset.
- Purchase Invoice item now debits the accrued liability account (no duplicate expense).
- Accrued-liability Journal Entry linked to settlement via `settlement_journal_entry` and reversed on cancel.
- `recompute_period_slabs`: aggregates attainment per beneficiary within each open Commission Period and updates Commission Target attainment.
- `get_eligible_commission_entries`: whitelisted method for automatic fetching of eligible Commission Entries with balance enrichment.

## Collection eligibility vs payout separation

- `_insert_entry`: at invoice submission, `collection_eligible_amount = 0`, `payable_amount = 0`, `outstanding_amount = 0`, `status = "Earned"`. Only `earned_commission` is recorded.
- Added `accrual_status`, `collection_status`, `settlement_status` fields to Commission Entry for independent tracking.
- `update_commission_payment_status`: recalculates collection eligibility from all submitted Payment Entries (idempotent, supports multiple payments and cancellation). Uses `get_allocated_amount_in_transaction_currency` for currency conversion. `outstanding = collection_eligible - settled`.
- `_available_balance_for_entry`: available = min(collection_available, accrual_available) where collection_available = eligible - reserved - settled, accrual_available = accrued - reclassified.
- Calculation snapshot enhanced with `calculation_method`, `rate_percentage`, `fixed_amount`, `basis_after_split`, `conversion_rate`, `commission_currency`, `split_source`.

## Commission Collection Event (audit trail)

- Added `Commission Collection Event` child DocType (table on Commission Entry) for immutable audit records of each payment event.
- `update_commission_payment_status`: creates one event per matching Payment Entry Reference row, with proportional eligibility delta distribution (last reference gets rounding remainder).
- Idempotency via unique `event_key` (entry + payment_entry + payment_reference + event_type) enforced at DB level; duplicate events are skipped.
- Currency fields: `party_account_currency`, `invoice_currency`, `commission_currency`, `company_currency` with corresponding monetary amounts (`allocated_amount`, `invoice_currency_allocated`, `company_currency_allocated`, `eligibility_delta`).
- Event creation is mandatory (fail-closed): if event insertion fails, the entire Payment Entry transaction rolls back.
- Child DocType controller (`commission_collection_event.py`) enforces immutability: `validate`, `before_save`, and `on_trash` reject any modification or deletion.
- `collection_events` added to Commission Entry `validate` immutable_fields list.
- Concurrency safety: invoice-level `FOR UPDATE` lock acquired in `before_submit`/`before_cancel` hooks (sorted invoice order) before Payment Entry row is saved; cumulative collection uses locking read on reference rows only (no PE parent locks); Payment Entry status read via `FOR UPDATE NOWAIT` with retry.
- Payment Entry type guard: only `Receive` + `Customer` entries are processed.
- Fail-closed historical calculation: invalid exchange rates, missing invoices, or retrieval failures abort the transaction.
- `is_reversal` flag set when payment is cancelled.

## Notes for coders

- Hook registration lives in `hooks.py` (`doc_events`, `scheduler_events`); run `bench migrate` + restart after changes.
- Commission is stored in company currency; transaction currency and exchange rate are persisted for audit.
- Reversal is append-only; never delete generated `Commission Entry` records.
- `Commission Collection Event` records are immutable once created.
