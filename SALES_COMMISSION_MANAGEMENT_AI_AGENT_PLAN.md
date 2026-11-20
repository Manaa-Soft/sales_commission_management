# Sales Commission Management — AI Agent Remediation Plan

**Repository:** <https://github.com/Manaa-Soft/sales_commission_management>  
**Application:** ERPNext / Frappe v15 and v16  
**Purpose:** Correct financial, security, calculation, settlement, audit, and testing weaknesses in a controlled sequence.

---

## 0. Instructions to the AI coding agent

You are working on a financial application. Treat every commission amount, allocation, reversal, accrual, settlement, and payout as an accounting-critical operation.

### 0.1 Mission

Inspect the current repository, verify the issues listed in this plan against the **current checked-out code**, then implement the confirmed corrections phase by phase. Preserve the existing commission subledger architecture wherever it is sound. Do not perform a broad rewrite merely to make the code look cleaner.

Execute the phases in order. A phase is complete only when its acceptance criteria and tests pass. Do not move to the next phase while the current phase has unresolved critical failures.

Do not stop for routine approval between phases. Stop and report a blocker only when:

- A required accounting policy is unknown and cannot safely be inferred from the current implementation or documentation.
- A change would require a destructive data migration or could alter historical financial records.
- The correct ERPNext/HRMS payment lifecycle cannot be established from the installed version or supported source.
- A required integration test cannot be run and the missing environment materially affects the safety of the change.
- Existing data is inconsistent and an automatic repair could change financial balances.

When blocked, explain the exact decision or information required. Do not guess.

### 0.2 Safety and repository rules

1. Start by checking `git status`, current branch, and current commit SHA.
2. Never overwrite uncommitted user changes. If the working tree is dirty, inspect and preserve those changes before proceeding.
3. Work on a dedicated branch, not directly on `main`, unless the user explicitly instructs otherwise.
4. Do not push to the remote unless explicitly requested.
5. Do not connect to or modify a production database. Use tests and a disposable/staging site only.
6. Never delete historical Commission Entries, Collection Events, accrual allocations, payout records, or accounting references to make a test pass.
7. Never rewrite ERPNext/Frappe core files.
8. Do not add custom fields or new DocTypes until existing fields and structures have been inspected and shown to be insufficient.
9. Keep changes compatible with the Frappe/ERPNext versions the repository actually supports. Do not rely on APIs available only in one major version without a compatibility guard.
10. Do not silently swallow accounting, permission, database, or currency-conversion errors.
11. Do not mark a commission as paid because a settlement, Purchase Invoice, Additional Salary, or Salary Slip was merely created or submitted.
12. Every financial behavior change must have regression tests.
13. Preserve existing locking, idempotency, immutable-event, and append-only reversal improvements unless a verified flaw requires changing them.
14. Do not claim a test passed unless it was actually executed and its result was inspected.
15. Before finishing a phase, inspect the complete diff for unrelated changes, security regressions, incorrect currency handling, and migration risks.

### 0.3 Implementation approach

For each phase:

1. Re-read the relevant current source files and DocType JSON.
2. Trace callers, hooks, reports, API endpoints, and database fields affected by the change.
3. Write a short implementation plan listing exact files/functions and risks.
4. Implement the smallest coherent change.
5. Add tests before or alongside the fix.
6. Run targeted tests, then the app test suite where practical.
7. Run Python compilation/lint checks available in the repository.
8. Inspect the final diff and search for other usages of changed functions/fields.
9. Update documentation when behavior, fields, lifecycle, or accounting semantics change.
10. Produce a phase report using the format in Section 10.

If the repository already contains a fix, do not re-implement it. Verify the code and tests, record it as already fixed, and continue.

---

## 1. Baseline architecture and financial rules

### 1.1 Areas to inspect

Inspect the current versions of at least:

- `hooks.py`
- `sales_commission_management/doctype/commission_entry/commission_entry.py`
- `sales_commission_management/doctype/commission_entry/commission_entry.json`
- `sales_commission_management/doctype/commission_collection_event/`
- `sales_commission_management/doctype/commission_settlement/commission_settlement.py`
- `sales_commission_management/doctype/commission_accrual_batch/commission_accrual_batch.py`
- `sales_commission_management/doctype/commission_period/commission_period.py`
- `sales_commission_management/doctype/commission_settings/commission_settings.py`
- `sales_commission_management/doctype/commission_settings/commission_settings.json`
- Commission Plan, Commission Rule, Commission Contributor, Commission Target, and account-mapping controllers/DocType JSON.
- All whitelisted API methods, reports, scheduler jobs, and tests that read or change commission balances.
- Relevant documentation under `docs/` and the README.

Do not limit the inspection to the files named in this plan. Search the entire repository for references to changed fields, methods, hooks, and event types.

### 1.2 Establish the actual currency contract first

The repository's documentation and fields may use terms such as commission currency, transaction currency, and company currency differently. Derive the real current behavior from source code, DocType field definitions, accounting documents, and tests before changing it.

Document these separately:

- **Transaction currency:** currency of the source Sales Invoice/POS Invoice.
- **Commission currency:** currency in which the commission amount/rule is expressed.
- **Company currency:** the company's base accounting currency.
- **Settlement currency:** currency in which the commission obligation is settled, if configurable.
- **Payment currency:** currency of the actual beneficiary payment.

For every amount, identify its currency. For every conversion, identify the rate, direction, effective date, source, and stored snapshot. Do not convert an amount twice. Do not assume a rate of `1.0` is invalid when both currencies are identical.

Use ERPNext's supported currency and accounting APIs consistently. Do not introduce a second exchange-rate policy that conflicts with ERPNext. Follow the accounting date and rate policy already adopted by the application unless the audit proves it is defective; document any required policy choice.

### 1.3 Financial invariants

Make these invariants explicit in code comments/docs and enforce them in validation/services/tests:

1. A source earning event and beneficiary allocation must not generate duplicate commission entries.
2. A customer collection event must not make more commission eligible than the applicable net earned balance and the configured collection policy allow.
3. Settlement reservations must not exceed eligible, unreserved commission.
4. Actual payout allocations must not exceed the remaining amount of the linked settlement.
5. A settlement being submitted is not evidence that money was paid.
6. A cancellation/reversal must reverse the original financial effect using the relevant original accounting snapshot, not an unrelated current rate.
7. Every financial amount must have an unambiguous currency.
8. Company-currency GL entries must balance in company currency.
9. Historical posted events must remain traceable; corrections must use reversals/adjustments rather than destructive edits.
10. The same balance must not be independently maintained in multiple places without a defined source of truth and reconciliation.
11. No cross-company or cross-beneficiary settlement is allowed unless explicitly supported by a documented business rule.
12. Closed-period changes must follow an explicit reopen/adjustment policy and leave an audit trail.

Also document the intended balance formula. At minimum distinguish:

- Gross earned.
- Earned reversals/returns.
- Net earned.
- Collection eligible.
- Reserved for submitted settlements.
- Accrued in GL.
- Reclassified to payable.
- Actually paid.
- Recoverable after post-payment reversal.
- Available for a new settlement.

Do not force a formula that conflicts with the app's intended accrual-gated or collection-gated policy. Define the intended policy precisely and use it consistently in all entry, settlement, report, and API calculations.

---

## Phase 0 — Baseline audit and regression-test foundation

**Goal:** Establish the actual state of the current branch before code changes.

### Tasks

1. Record repository, branch, current commit SHA, and working-tree state.
2. Record supported Frappe, ERPNext, and HRMS versions from app metadata and CI/configuration.
3. Map the complete commission lifecycle:
   - Sales Invoice/POS Invoice submission.
   - Commission calculation and contributor splitting.
   - Customer Payment Entry submission/cancellation.
   - Collection eligibility and immutable Collection Events.
   - Accrual batch and Journal Entry posting.
   - Settlement reservation and linked payroll/supplier documents.
   - Actual beneficiary payout.
   - Returns, invoice cancellation, accrual reversal, and recovery.
   - Period close/reopen and reporting.
4. Map all currency fields, currency conversions, and GL postings.
5. Verify every finding in this plan as one of:
   - Confirmed in current code.
   - Already fixed and adequately tested.
   - Partially fixed.
   - Not reproducible.
   - Requires accounting-policy clarification.
6. Map each finding to exact current files, functions, fields, hooks, and tests.
7. Run the current app test suite and record the actual result. Do not fix unrelated failures during baseline collection.
8. Identify missing tests for critical financial paths.
9. Inspect recent history for locking, retry, idempotency, and immutable event changes. Preserve verified improvements.

### Deliverables

- Architecture map.
- Financial lifecycle diagram.
- Currency flow map.
- Finding verification table.
- Baseline test report.
- Phase-by-phase implementation file list.
- Backward-compatibility and migration risks.

### Phase 0 exit criteria

- Current code state is known.
- Findings are revalidated against current `HEAD`.
- Currency semantics are documented.
- No source or database records have been changed during the audit.

Proceed to Phase 1 when these criteria are met.

---

## Phase 1 — Critical defects, API authorization, and settings

**Goal:** Prevent known failures and unauthorized access before expanding accounting behavior.

### 1.1 Accrual exchange-rate validation

**Inspect:** `commission_accrual_batch/commission_accrual_batch.py`, especially `_compute_company_amounts()` and its conversion helpers.

Previously identified risk: the condition around `commission_to_company_rate` appeared inverted, potentially throwing when a valid non-zero rate exists and attempting fallback only when the rate is absent. Recheck current code; do not assume this defect still exists.

Required behavior:

- Accept valid positive rates, including `1.0` for identical currencies.
- Derive a rate only when the stored rate is missing/invalid and the approved policy permits derivation.
- Fail clearly if no valid rate can be established.
- Reject zero, negative, non-finite, or otherwise invalid rates.
- Do not silently replace a historical snapshot with today's rate.

**Tests:** same-currency rate 1.0, valid foreign rates, missing rate with fallback, missing rate without fallback, zero/negative rate, invalid currency pair.

### 1.2 Whitelisted API authorization

**Inspect at least:**

- `get_eligible_commission_entries()` in settlement code.
- `fetch_unaccrued_entries()` in accrual code.
- All other `@frappe.whitelist()` methods that retrieve or mutate commission, settlement, payout, accrual, or beneficiary data.

Previously identified risk: methods using `frappe.get_all()` may bypass document user permissions. Verify actual DocType permissions and all caller contexts.

Implement explicit server-side authorization:

- Verify required role/permission for commission management or accounting operations.
- Enforce company access for every requested company.
- Enforce beneficiary ownership/scope for non-privileged users.
- Validate every requested Commission Entry, not just the list shown in the UI.
- Re-check authorization and eligibility when a settlement/accrual document is saved and submitted.
- Use `frappe.get_list()` where suitable; if `get_all()` is required for an internal privileged operation, explicitly validate access before returning data.
- Return an empty result or deny access when a non-privileged user has no resolvable beneficiary scope. Never interpret a missing scope as permission to return all rows.
- Do not rely on client-side filters or hidden buttons.

**Tests:** privileged role, accounting role, unauthorized user, salesperson mapped to own beneficiary, missing beneficiary mapping, tampered beneficiary parameter, unauthorized company, unauthorized entry during submit.

### 1.3 Commission Settings imports and validation

**Inspect:** `commission_settings/commission_settings.py`.

Previously identified risk: `_validate_account_mappings()` called `frappe.throw()` and `_()` without importing them. Recheck current code.

Ensure imports are explicit and validation tests cover:

- Duplicate enabled company mapping.
- Disabled duplicate mapping.
- Missing company.
- Invalid account/company relationship.
- Required accounts missing for the selected accounting mode.

### 1.4 Enforce global/source settings

Inspect `hooks.py`, settings controllers, and all event handlers.

Verify actual behavior for:

- Master `enabled` setting.
- Sales Invoice enablement.
- POS Invoice enablement.
- Payment Entry/collection processing enablement.
- Native HRMS overlap guard.

A disabled earning source must not create new commission entries. However, turning off the engine must not prevent required reversals, cancellations, or payout corrections for previously generated financial obligations.

### Phase 1 exit criteria

- Valid accrual exchange rates work.
- Invalid rates fail safely.
- Financial APIs enforce role, company, and beneficiary boundaries.
- Settings validation does not raise `NameError`.
- Master and source-specific settings behave as documented.
- Existing collection-event idempotency and immutability tests still pass.

---

## Phase 2 — Multi-currency accounting and GL correctness

**Goal:** Make all accounting documents and reversals use the correct currency and conversion snapshots.

### 2.1 Define and enforce one currency contract

Using the Phase 0 currency map, define the canonical meaning of every stored monetary field. Avoid renaming or migrating fields unless necessary. Document whether each amount is in commission currency, transaction currency, party account currency, or company currency.

For every conversion, persist or reliably reference:

- From currency.
- To currency.
- Rate and direction.
- Effective date.
- Rate source/policy.
- Original amount.
- Converted amount.

Use the same conversion utility/policy across accrual, settlement, reversal, and reporting where their accounting basis is the same.

### 2.2 Accrual Journal Entries

**Inspect:** accrual computation, allocations, Journal Entry construction, and reversal links.

Ensure:

- Accrual allocation retains the commission amount and currency.
- GL debit and credit are in company currency.
- Foreign-currency commission is converted exactly once.
- Company-currency equivalent is stored for reconciliation.
- The posted Journal Entry is linked to the batch and relevant Commission Entries/allocations.
- Rounding uses the correct currency precision.
- The batch cannot be submitted with missing mappings, rates, or unbalanced entries.

### 2.3 Additional Salary settlement

**Inspect:** `_create_additional_salary()` and the installed HRMS version's currency/payroll semantics.

Do not assume `self.total_payable` is already in the currency expected by Additional Salary.

Determine:

- Employee/company association.
- Payroll/company currency expectations.
- Whether Additional Salary stores an amount in company currency or another currency in the supported HRMS version.
- How the amount is carried into Salary Slip and payroll GL.

Convert explicitly when required, preserve source amount/rate snapshots, and avoid double conversion. Do not invent a payroll-currency field if HRMS does not support the intended workflow.

### 2.4 Purchase Invoice settlement

**Inspect:** `_create_purchase_invoice()` and ERPNext supplier accounting behavior.

Ensure:

- Purchase Invoice currency is deliberate and valid for the supplier/company.
- Item rate and document currency agree.
- Conversion rate is set/derived according to ERPNext policy.
- Company-currency payable is correct.
- Settlement and Purchase Invoice retain clear links and amounts.
- Partial supplier payments later reconcile against the actual Purchase Invoice outstanding.

### 2.5 Invoice cancellation and accrual reversal

**Inspect:** `reverse_commission_entries()` and the accrual/reclassification allocation lookup functions.

Previously identified risk: commission-currency amounts may have been passed directly to company-currency Journal Entry lines. Recheck current implementation.

Required behavior:

- Reverse the original posted company-currency accrual amount (or exact allocation) using its original conversion snapshot.
- Do not apply today's FX rate to reverse a historical GL entry unless a documented accounting policy requires it.
- Link reversal to original accrual and Commission Entry.
- Handle partial accrual/reclassification accurately.
- If a valid zero accrual exists, handle it explicitly.
- If an expected posted accrual exists but its mapping/allocation cannot be found, fail closed and report the inconsistency.
- Missing required accounts/rates must not silently skip the reversing Journal Entry.
- Ensure debit/credit equality in company currency.

### 2.6 Currency integration tests

Cover same-currency and foreign-currency cases for:

- Sales invoice commission.
- Collection events and partial collections.
- Accrual Journal Entry.
- Reclassification.
- Settlement via Additional Salary.
- Settlement via Purchase Invoice.
- Supplier payment.
- Accrual reversal.
- Invoice cancellation after accrual.
- FX snapshot retained after exchange-rate changes.

### Phase 2 exit criteria

- No amount is ambiguous about its currency.
- Commission currency and company currency are never confused.
- Every GL posting is correct and balanced in company currency.
- Payroll and supplier documents use their actual currency semantics.
- Reversals reverse the original company-currency accounting amount.

---

## Phase 3 — Actual beneficiary payout lifecycle

**Goal:** Distinguish commission settlement processing from money actually paid to the beneficiary.

### 3.1 Define the lifecycle and source of truth

Document and implement distinct states/amounts for:

- Draft settlement.
- Submitted/reserved settlement.
- Partially paid settlement.
- Fully paid settlement.
- Cancelled settlement.
- On-hold settlement, if supported.
- Actual paid amount.
- Remaining payable.
- Recoverable amount after post-payment reversal.

A submitted Settlement, Purchase Invoice, Additional Salary, or Salary Slip is not proof of payment.

### 3.2 Inspect existing data structures before adding anything

Search for existing payout fields, links, allocation tables, payment hooks, or event DocTypes. Reuse/extend them if they can represent partial payments, cancellations, and audit history safely.

If the existing structures cannot do this, propose the smallest append-only payout event/allocation structure. Potential fields include:

- Commission Settlement.
- Payment Entry.
- Payment Entry Reference row.
- Payroll payment/GL reference where applicable.
- Posting date.
- Payment currency and amount.
- Company-currency amount.
- Exchange-rate snapshot.
- Event type (payment/reversal/adjustment).
- Idempotency key.
- Reversal link/original event reference.

Do not add a new DocType until the inspection demonstrates that the existing model is insufficient.

### 3.3 Supplier payout integration

For external sales partners:

- Link the settlement to its Purchase Invoice.
- Process only actual supplier payment allocations against that linked invoice.
- Support partial payments and multiple Payment Entries.
- Record payment and cancellation/reversal events idempotently.
- Ignore unrelated supplier payments.
- Reconcile payment-currency and company-currency amounts.

### 3.4 Employee payroll payout integration

For internal salespeople:

- Link the settlement to Additional Salary and the relevant payroll documents.
- Verify the installed HRMS workflow for Salary Slip submission, payroll accrual, and actual disbursement.
- Record actual disbursement based on a reliable ERPNext accounting/payment reference, not merely on Additional Salary creation.
- Support partial/separate disbursement if supported by the payroll workflow.
- Do not mark an employee commission paid merely because a Salary Slip is submitted or a payroll Journal Entry is created unless that event is the explicitly approved definition of paid in the company's accounting process.

If ERPNext/HRMS does not expose a reliable actual employee payment link for the configured workflow, document the limitation and implement a deliberate approved integration point rather than guessing.

### 3.5 Partial payments and cancellation

Use a consistent formula for the linked settlement:

`Outstanding = Submitted Settlement Amount - Valid Payment Allocations + Reversed Payment Allocations`

Apply currency conversion using the agreed payout policy. Prevent payment allocations from exceeding outstanding payable, except where an explicitly modelled overpayment/advance is permitted.

Example: settlement 100; payment 30; payment 40; outstanding 30; after final payment 30; status becomes Paid.

### 3.6 Returns after payout and recoveries

If an earning is reversed after the beneficiary has already been paid:

- Preserve the original payout event.
- Create a recoverable negative balance/adjustment linked to the original commission and return.
- Do not silently reduce historical `paid_amount` as though payment never occurred.
- Support approved deduction against future commissions or a separate recovery process.
- Do not create an unlawful payroll deduction automatically; follow applicable company policy and legal constraints.

### Phase 3 exit criteria

- Submitted is not confused with Paid.
- Partial supplier and payroll payments are represented correctly.
- Cancellation of a payout reverses its allocation without deleting history.
- Return after payout creates a traceable recoverable amount.
- Settlement, payout records, and ERPNext payable/payroll records reconcile.

---

## Phase 4 — Commission calculation accuracy and configuration completeness

**Goal:** Correct calculation bases and ensure configured rules are actually honored.

### 4.1 Row-level COGS and gross-profit commission

**Inspect:** `_resolve_posted_cost()` and all callers in `commission_entry.py`.

Previously identified risk: querying Stock Ledger Entries broadly by voucher and item code may aggregate cost for repeated invoice rows and assign the same total to each row. Recheck current code.

Implement row-level cost tracing:

1. Identify the source invoice item row.
2. Trace its linked Delivery Note item rows where stock movement is delivery-based.
3. Match Stock Ledger Entries using the actual row linkage and voucher chain.
4. Support partial deliveries and multiple Delivery Notes.
5. Handle returns, cancellation, stock reposting, and valuation changes safely.
6. Ensure the sum of row allocations reconciles to the relevant stock valuation/cost basis.
7. Define behavior when a reliable row mapping cannot be obtained; do not silently use a voucher-wide total for every repeated row.

Test repeated item codes on one invoice, partial delivery, multiple delivery notes, return, and stock repost.

### 4.2 Contributor behavior

Inspect Contributor DocType, controller, and calculation paths. Verify that configured fields are actually used:

- `commission_rate` override.
- `commission_multiplier`.
- `split_percentage`.
- `require_employee`.
- `require_supplier`.
- Beneficiary type and eligibility.

Document calculation order, for example: base → rule rate/fixed amount → split → multiplier → currency conversion, but use the business semantics already intended by the app and make the order consistent everywhere.

Add validation for duplicate contributors, invalid/missing beneficiary, missing required employee/supplier, invalid percentages, and total split policy. If the policy requires 100%, enforce it within a documented rounding tolerance.

### 4.3 Fixed currency plan policy

Inspect Commission Plan DocType JSON/controller and `_resolve_commission_currency()`.

If a Fixed Currency option exists, ensure it has a corresponding mandatory currency field and the calculation honors it. Do not silently fall back to company currency for an explicitly configured fixed-currency plan. Preserve compatibility for existing plans with missing values through a safe migration/default policy.

### 4.4 UOM normalization

Inspect `commission_uom` and fixed-per-unit calculation.

Normalize transaction quantities to the configured commission UOM using ERPNext item UOM conversion factors. Test equivalent quantities (e.g. 5 boxes of 100 pieces versus 500 pieces) produce the same commission. Handle missing/invalid conversion factors by failing validation rather than using raw quantities.

### 4.5 Freight, discounts, and free-item policy

Inspect `include_freight`, `include_free_items`, grant-commission flags, discount handling, and all calculation basis fields.

Implement or explicitly remove/deprecate settings that are not supported. Do not leave controls in the UI that have no effect.

Define whether commission base includes:

- Item net amount.
- Discount.
- Taxes.
- Freight.
- Free items.
- Returns.

Avoid including freight or discount twice. Store a calculation snapshot sufficient to reproduce the result.

### 4.6 Plan/rule resolution

Verify deterministic rule selection, plan priority, Item → deepest Item Group → All Items matching, equal-priority ambiguity handling, date validity, and stacking policy. If multiple candidates tie, do not rely on unspecified database ordering.

### Phase 4 exit criteria

- Gross-profit calculation allocates COGS to the correct source row.
- Contributor settings affect calculations as documented.
- Split rules are validated.
- Fixed currency and UOM settings behave as configured.
- Freight, discount, tax, and free-item policies are explicit and tested.
- Plan/rule selection is deterministic.

---

## Phase 5 — Sales Invoice, POS Invoice, returns, and source-document integration

**Goal:** Ensure each supported source document is stored and reversed using its actual DocType and row references.

### 5.1 POS source-type consistency

**Inspect:** `hooks.py`, `_insert_entry()`, commission generation, reversal, source Link fields, and idempotency keys.

Previously identified risk: POS Invoice hooks may call code that hardcodes `Sales Invoice` as `source_doctype` or relies on Sales Invoice-only link fields. Recheck current code.

Required behavior:

- Store the actual source DocType and source name.
- Populate Sales Invoice-specific Link fields only for Sales Invoice.
- Populate POS-specific Link fields only when the DocType supports them.
- Use a valid Dynamic Link or supported source-link model if the current DocType cannot represent both.
- Include the actual source DocType in idempotency keys.
- Ensure returns and cancellations resolve the correct original source.

Do not blindly change string literals without reviewing DocType field types and database constraints.

### 5.2 Earning event policy

Verify the supported earning events, especially any configured `On Delivery` option. If Delivery Note earning is offered but no hook/implementation exists, either implement it end-to-end with idempotency and reversal or disable/remove the unsupported option with a clear migration/documentation strategy.

### 5.3 Test source-document workflows

For Sales Invoice and POS Invoice independently, test:

- Submit.
- Cancel.
- Amend.
- Full return.
- Partial return.
- Multiple identical item codes.
- Multiple contributors.
- Customer payment and payment cancellation.
- Source-document link correctness.
- Duplicate hook execution/idempotency.

### Phase 5 exit criteria

- Each entry links to the actual source DocType and source document.
- POS does not depend on invalid Sales Invoice links.
- Returns and cancellations reverse the correct original records.
- Every advertised earning event is implemented or clearly unavailable.

---

## Phase 6 — Settlement beneficiary controls and Commission Periods

**Goal:** Prevent mixed-beneficiary settlement, duplicate reservations, and closing unreconciled periods.

### 6.1 Settlement validation

**Inspect:** `_validate_entries()`, beneficiary resolution, currency validation, available-balance calculation, submit/cancel lifecycle, and linked document creation.

Before submission, enforce:

- One intended beneficiary per settlement, unless a documented group settlement design explicitly supports otherwise.
- Beneficiary type matches settlement route.
- Employee belongs to the intended salesperson/beneficiary where employee settlement is used.
- Supplier belongs to the intended sales partner/beneficiary where supplier settlement is used.
- One company per settlement unless cross-company settlement is deliberately supported.
- Compatible commission/settlement currencies.
- Every entry is eligible and belongs to the requested beneficiary/company.
- Requested amount does not exceed available balance.
- Active reservations and prior settlements are included.
- Employee/supplier requirements are enforced.

A blank beneficiary on the parent Settlement must not allow entries for different beneficiaries to be combined accidentally.

### 6.2 Reservation concurrency

Use a consistent locking order for affected Commission Entry rows. Verify idempotency and transaction boundaries. Test two concurrent settlements attempting to reserve the same entry. The total reservation must never exceed eligible available amount.

Do not introduce unnecessary parent-row locks or change the existing Payment Entry lock strategy without a demonstrated concurrency issue and tests. Keep lock acquisition sorted and document the lock order.

### 6.3 Commission Period reconciliation

**Inspect:** `commission_period/commission_period.py`, particularly `_check_reconciliation()`, `close_period()`, and `reopen_period()`.

Previously identified risk: reconciliation was a placeholder. Recheck current code.

Implement close-period checks for:

- Unprocessed earning/source events.
- Unresolved collection-event allocations or reversals.
- Incomplete/failed accrual batches.
- Accrual allocations that do not reconcile to posted Journal Entries.
- Unresolved settlement reservations.
- Unmatched payout events.
- Missing reversals or failed cancellation processing.
- Entries with missing/incorrect period assignment.
- Material ledger-to-GL discrepancies.

A period must not close if material discrepancies remain. Provide a documented exception workflow only where justified, with approver, timestamp, reason, and affected records.

### 6.4 Period assignment and reopening audit

Assign Commission Period consistently using the documented accounting date/event date policy. Define treatment of late collections, returns, and reversals related to closed periods. Do not silently mutate a closed period's historical totals.

Reopen must:

- Require a reason.
- Require an authorized role.
- Preserve each reopen/close event in an audit history, not merely overwrite one set of fields.
- Record user and timestamp.
- Re-run reconciliation before re-closing.

### Phase 6 exit criteria

- Mixed-beneficiary settlement is rejected.
- Wrong employee/supplier linkage is rejected.
- Concurrent reservations cannot over-allocate.
- Period closing runs real reconciliations.
- Reopen/close actions are permission-controlled and auditable.

---

## Phase 7 — Subledger-to-GL and payout reconciliation

**Goal:** Make every balance explainable from source events and reconcilable to ERPNext accounting.

### 7.1 Define balance derivation

Identify the authoritative ledger/events. Avoid independently editable balance fields where they can be derived. If cached totals are retained for performance, ensure they are updated transactionally and have a reconciliation/rebuild mechanism.

Provide a documented derivation for:

- Gross earned.
- Earned reversals.
- Net earned.
- Collection eligible.
- Reserved.
- Accrued.
- Reclassified.
- Actually paid.
- Recoverable.
- Available.

All balances must be computed in a defined currency. Reports must not add values with different currencies without conversion.

### 7.2 Accrual GL reconciliation

For each company and accounting period, reconcile the commission liability control balance to its posted transactions. At a conceptual level:

`Closing liability = Opening liability + posted accruals - posted reclassifications - posted reversals +/- approved adjustments`

Use actual debit/credit signs and account mappings from the implementation. Reconcile against General Ledger in company currency and account for legitimate opening balances/adjustments.

### 7.3 Settlement/payable reconciliation

For external beneficiaries, trace:

`Commission Entry → Settlement → Purchase Invoice → Supplier Payment Entry → Outstanding payable`

For internal beneficiaries, trace:

`Commission Entry → Settlement → Additional Salary → Salary Slip/payroll accounting → actual disbursement reference`

Document any supported HRMS limitation. Do not show an amount as actually paid if only a payroll accrual exists.

### 7.4 Audit reports and exception reporting

Implement or improve reports as needed for:

- Commission Ledger Reconciliation.
- Commission Accrual vs GL.
- Settlement vs Actual Payments.
- Outstanding Commission Payables.
- Recoverable Commission.
- Commission Currency Exposure.
- Unallocated Collection Events.
- Unmatched Payout Events.
- Closed Period Exceptions.

Every report must respect role, company, and beneficiary permissions. Clearly show the currency for each monetary figure.

### 7.5 Traceability

A user with appropriate permissions should be able to trace a Commission Entry to:

- Source sales document and item row.
- Calculation snapshot and contributor split.
- Customer Collection Events.
- Accrual batch/allocation and Journal Entry.
- Reclassification.
- Settlement.
- Purchase Invoice or Additional Salary.
- Actual payout event/payment reference.
- Return, cancellation, or recovery reversal.

### Phase 7 exit criteria

- Subledger and GL reconcile by company, account, and period.
- Settlement documents reconcile with actual payables/payroll.
- Paid and recoverable values are auditable.
- Reports show currency and enforce access controls.

---

## Phase 8 — Full regression, concurrency, migration, and production-readiness testing

**Goal:** Prove the corrected lifecycle works end-to-end on supported ERPNext/Frappe versions.

### 8.1 Test suite organization

Follow the repository's actual Frappe test-discovery conventions. Prefer focused modules such as:

- `test_commission_calculation.py`
- `test_commission_contributors.py`
- `test_commission_collection.py`
- `test_commission_accrual.py`
- `test_commission_currency.py`
- `test_commission_settlement.py`
- `test_commission_payout.py`
- `test_commission_reversal.py`
- `test_commission_period.py`
- `test_commission_pos.py`
- `test_commission_security.py`
- `test_commission_concurrency.py`
- `test_commission_reconciliation.py`

These are suggested logical groupings, not a requirement to create duplicate or incorrectly placed test packages.

### 8.2 Mandatory integration scenarios

At minimum test:

1. Sales Invoice submission creates correct entries.
2. POS Invoice submission creates correct entries.
3. Multiple contributors split correctly.
4. Duplicate event processing does not duplicate entries.
5. Partial customer payment updates eligibility correctly.
6. Multiple customer payments update cumulative eligibility correctly.
7. Payment Entry cancellation reverses eligibility correctly.
8. Concurrent customer payments do not create duplicate/lost collection events.
9. Foreign-currency commission stores correct snapshots.
10. Foreign-currency accrual creates a balanced company-currency Journal Entry.
11. Accrual cancellation reverses exact original company-currency amount.
12. Partial settlement reserves only the intended amount.
13. Two concurrent settlements cannot over-reserve.
14. Partial supplier payment updates actual paid/outstanding balances.
15. Payroll commission follows the actual supported payment lifecycle.
16. Payment cancellation reverses payout allocations.
17. Partial return reverses the correct quantity/commission.
18. Return after payout creates recoverable balance.
19. Invoice cancellation after accrual creates the correct GL reversal.
20. Repeated invoice item rows receive row-specific COGS.
21. Multiple Delivery Notes and partial deliveries calculate correct costs.
22. Equivalent UOM quantities produce equivalent commission.
23. Freight/discount/free-item rules behave as configured.
24. Unauthorized API calls cannot read or mutate another user's/company's data.
25. Closed periods reject invalid closing and unauthorized changes.
26. Reconciliation reports agree with source ledger and GL.
27. Migration preserves historical commission, event, accrual, settlement, and payout data.

### 8.3 Concurrency and locking

Specifically test:

- Two simultaneous Payment Entries against the same invoice.
- Payment submission and cancellation overlap.
- Two simultaneous settlement submissions for the same entry.
- Settlement cancellation while payout is being recorded.
- Repeated event handling.
- MariaDB lock timeout/retry behavior.

Preserve deterministic lock ordering. Inspect actual Frappe/MariaDB exception wrapping. Retry only recognized transient lock-contention errors; re-raise unrelated exceptions. Do not retry an operation in a way that can duplicate financial events.

### 8.4 Version matrix

Run the supported app tests on each supported major version (at least the declared ERPNext/Frappe v15 and v16 combinations if both are claimed). Record exact versions and outcomes. Do not claim compatibility with an untested version.

### 8.5 Migration and deployment validation

On a disposable/staging site:

1. Take a backup.
2. Install/update the app using the documented process.
3. Run migrations.
4. Verify DocType schema changes.
5. Verify historical records remain intact.
6. Run app tests.
7. Execute representative end-to-end accounting scenarios.
8. Reconcile GL and subledger.
9. Test rollback/recovery procedure where practical.

Never test a migration first on production.

### Phase 8 exit criteria

- All critical/high-priority tests pass.
- No unexplained GL/subledger difference remains.
- Permission tests pass.
- Concurrency tests pass without duplicate/lost allocations.
- Migration preserves existing data.
- Each supported version has a recorded test result.
- No unresolved critical or high-risk issue remains.

---

## 10. Required phase completion report

After each phase, report using this structure:

### Phase N — [Name]

**Status:** Complete / Blocked / Partially complete  
**Base commit:** `<sha>`  
**Resulting commit:** `<sha>` (if committed)  

**Findings verified:**
- List issues confirmed, already fixed, or not reproducible.

**Files changed:**
- Exact paths and short description of each change.

**Implementation details:**
- Explain important behavior changes and accounting decisions.

**Tests executed:**
- Exact commands.
- Number passed/failed/skipped.
- Relevant output or failure summary.

**Financial validation:**
- Explain how amounts, currencies, rates, GL entries, balances, or reservations were validated.

**Security validation:**
- Explain role/company/beneficiary permission checks where relevant.

**Migration/backward compatibility:**
- New fields/DocTypes, defaults, patches, existing-record handling, and risks.

**Remaining risks/blockers:**
- Be explicit. Do not describe untested behavior as complete.

**Next phase:**
- State the next phase only when the current phase exit criteria are met.

---

## 11. Final production-readiness checklist

The application is not production-ready merely because it compiles or because a few unit tests pass.

Production approval requires all of the following:

- [ ] All current critical and high-priority findings are fixed or formally accepted with documented mitigation.
- [ ] API authorization is tested for roles, companies, and beneficiaries.
- [ ] Commission, transaction, company, settlement, and payment currencies are unambiguous.
- [ ] Accruals and reversals are correct in company currency.
- [ ] Settlement submission is distinct from actual payout.
- [ ] Partial payouts and payment cancellations are supported.
- [ ] Returns after payout create traceable recoveries.
- [ ] COGS is correctly allocated to source rows.
- [ ] Contributor, fixed-currency, UOM, freight, and discount settings behave as documented.
- [ ] Sales Invoice and POS Invoice source links are correct.
- [ ] Beneficiary and company consistency is enforced at submission.
- [ ] Period closing performs actual reconciliation.
- [ ] Subledger balances reconcile to GL and payables/payroll.
- [ ] Concurrency and idempotency tests pass.
- [ ] Supported v15/v16 combinations are tested and recorded.
- [ ] Staging migration and historical-data preservation are verified.
- [ ] No unexplained financial differences remain.

**Final principle:** The objective is not simply to make every feature execute. Every commission amount must be explainable, traceable to its source, reproducible from its calculation snapshot, correctly represented in its currency, and reconcilable with ERPNext accounting.
