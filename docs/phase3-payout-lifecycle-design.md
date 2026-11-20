# Phase 3 — Actual Commission Payout Lifecycle Design Specification

**Document type:** Design specification only — no implementation authorized by this document
**Repository:** Manaa-Soft/sales_commission_management
**Branch reviewed:** feature/improvment_commission
**Design baseline:** e3ca9bc437227a710f2d366fc108ddac19ebc37b
**Status:** Proposed technical design for review before implementation

---

## 1. Objective

Make commission settlement distinguish between:

1. commission earned;
2. commission eligible for settlement after customer collection;
3. commission reserved in a submitted Commission Settlement;
4. a payable document or payroll amount being created/posted;
5. a beneficiary payout actually recorded; and
6. a payout later reversed or recovered because of a return, cancellation, refund, or correction.

The central rule is:

> A submitted Commission Settlement, Additional Salary, Salary Slip, or payroll accrual entry is not, by itself, proof that the beneficiary has been paid.

Phase 3 must provide an immutable, idempotent audit trail of payout and reversal events. Existing commission-earned, collection, and accrual ledgers must remain separate.

## 2. Findings from the current branch

### 2.1 What already exists

- Commission Settlement reserves eligible commission entries and creates one of the configured settlement documents.
- Supplier settlements create a Purchase Invoice for the selected Supplier.
- Employee settlements create and submit an Additional Salary linked back to the Commission Settlement.
- `_available_balance_for_entry()` combines collection eligibility, reservations, and (in Periodic Accrual mode) posted accrual availability.
- Customer Payment Entry hooks process Receive / Customer receipts and recalculate collection eligibility.
- Commission Collection Event is an immutable audit record and is a useful pattern for the payout-event controller.
- Commission Entry already has `paid_amount`, `settled_amount`, and `paid_delta` fields, but there is no payout-source handler in the inspected branch that records actual beneficiary payments into those fields.

### 2.2 The current gap

The settlement submit lifecycle creates a document and marks the settlement submitted. It does not prove that the Supplier was paid or that the Employee received payroll. The settlement's `outstanding` value is currently derived from the included amount, and the browser-side total calculation subtracts `total_paid` while the Python `_compute_totals()` currently sets `outstanding` to `total_payable`. These values must be given one server-authoritative meaning in Phase 3.

The current Payment Entry hooks do not process supplier Pay transactions as commission payouts. Their current purpose is customer collection eligibility and on-payment commission generation.

### 2.3 Important payroll distinction

Frappe HR's payroll instructions state that submitting Salary Slips creates the payroll accrual Journal Entry and does not pay employees. The Make Bank Entry action creates a bank Journal Entry; the official guide explicitly says that this records the payment in the books and does not itself transfer money to employees. Therefore:

- Additional Salary submitted = commission included for payroll processing;
- Salary Slip submitted = payroll amount calculated and accrued;
- Payroll accrual Journal Entry submitted = payroll liability booked;
- Payroll Bank Entry submitted = payment recorded in the accounting books;
- external payment confirmation / bank reconciliation = evidence that funds were actually disbursed.

Phase 3 must not label the first three as Paid. It must also distinguish a payroll Bank Entry posted in ERP from externally verified employee disbursement.

## 3. Domain terminology and invariants

Use these terms consistently in code, fields, reports, and user interface:

| Term | Meaning |
|---|---|
| Earned | Commission calculated from its source transaction. |
| Collection eligible | Amount unlocked by customer receipts according to the commission plan. This is not a beneficiary payout. |
| Reserved | Amount included in a submitted Commission Settlement and not yet released or paid. |
| Settlement document created | Additional Salary or Supplier Purchase Invoice exists and is submitted. This is not proof of payment. |
| Payroll accrued | Salary Slip / Payroll Entry accrual has posted the employee's payroll liability. This is not proof of payment. |
| Disbursement recorded | ERP has a submitted payout voucher or payroll bank-entry record. |
| Paid | A qualifying payout event exists under the configured evidence policy. |
| Reversed | A previously recorded payout has been compensated by a separate reversal event. The original event remains unchanged. |
| Recoverable | A negative commission entitlement exists after amounts already paid have been taken into account. This is a receivable/recovery balance, not a deletion or rewrite of historical payout. |

Mandatory invariants:

1. Customer receipts can change collection eligibility only. They must never increment beneficiary paid_amount or settled_amount.
2. A Commission Settlement submission reserves commission; it does not mark commission Paid.
3. Additional Salary submission does not mark employee commission Paid.
4. Salary Slip submission and Payroll Entry accrual do not mark employee commission Paid.
5. A payout event must reference an exact source document and, where the source has child allocations, the exact child row.
6. A single Payment Entry paying several invoices contributes only the amount allocated to the Purchase Invoice linked to the Commission Settlement. Never use the Payment Entry's whole paid/received amount as the commission payout amount.
7. Payout events are append-only. Cancellation, refund, chargeback, or correction creates a compensating event; it does not edit or delete the original event.
8. A return after a payout must preserve the payout event and create or increase a recoverable balance where the paid amount exceeds the revised commission entitlement.
9. All calculations must retain separate payment currency, settlement/commission currency, and company currency amounts. Never add amounts from different currencies.
10. All payout processing must be idempotent and safe under duplicate hooks and concurrent submissions.
11. Server-side calculations are authoritative. Browser-side calculations may preview values but must not define posted financial balances.

## 4. Proposed data model

### 4.1 New DocType: Commission Payout Event

Create a standalone DocType, not a child table. A payout can be sourced by documents that are outside the Commission Settlement tree, and a standalone record provides a stable audit identity and reversal history.

Suggested fields:

| Field | Type / intent |
|---|---|
| event_key | Data, unique, mandatory idempotency key. |
| event_type | Select: Payout, Payout Reversal, Recovery, Recovery Reversal, Manual Adjustment (manual adjustment only if separately approved). |
| settlement | Link to Commission Settlement. |
| settlement_item | Link to the exact Commission Settlement Item child row. |
| commission_entry | Link to the source Commission Entry. |
| company | Link to Company, copied and validated from the settlement/source. |
| beneficiary_type / beneficiary | Immutable beneficiary identity snapshot; retain Employee/Supplier/Sales Person/Sales Partner identity where applicable. |
| source_doctype / source_name | Exact source voucher, such as Payment Entry, Journal Entry, Bank Transaction, or an approved payroll confirmation record. |
| source_row | Exact source child-row identifier where applicable (especially Payment Entry Reference or salary detail row). |
| posting_date | Effective source posting date. |
| source_currency / source_amount | Original payment instrument amount and currency, where present. |
| settlement_currency / settlement_amount | Amount applied against the settlement in its settlement currency. |
| commission_currency / commission_amount | Amount credited/debited against commission entitlement. |
| company_currency / company_amount | Company-currency equivalent used for accounting/audit reconciliation. |
| fx_rate | Snapshot rate used for a specific from-currency to to-currency conversion. |
| fx_from_currency / fx_to_currency / fx_date / fx_source | Explicit orientation, date, and origin of the stored rate. Do not store a rate without its direction. |
| evidence_level | ERP Payment Voucher, Payroll Bank Entry, Bank Reconciled, or Manual Evidence (manual evidence subject to strict permissions and mandatory attachment/reference). |
| reversal_of | Link to the original Commission Payout Event for reversal events. |
| remarks / evidence_attachment | Reason and supporting evidence where needed. |

The exact DocType options and currency fields must be verified against Frappe v15 and v16 before implementation. Do not treat the table above as an instruction to add every field without reviewing framework conventions.

### 4.2 Immutability and lifecycle

Follow the existing Commission Collection Event approach:

- reject edits to an existing event;
- reject deletion;
- create events only from trusted server-side payout handlers;
- do not allow ordinary users to insert or edit payout events directly;
- do not cancel an event to represent a payment cancellation; create a reversal event;
- enforce a database-unique event_key as the final idempotency guard.

Prefer submitted events if a submittable DocType is used. The implementation must settle the exact docstatus lifecycle during the compatibility spike. Regardless of lifecycle choice, historical event fields must be immutable.

### 4.3 Allocation model

One payout source can cover multiple settlement items. The design must create a deterministic allocation from the source payment amount to each affected Commission Settlement Item.

For Supplier settlements, one Payment Entry Reference normally identifies the Purchase Invoice for the settlement, but that Purchase Invoice may represent several Commission Settlement Items. The Payment Entry Reference amount therefore needs to be allocated to those items.

Proposed allocation rule for the MVP:

1. calculate each item's remaining unpaid settlement amount;
2. allocate the source's applied settlement amount proportionally to each item's remaining unpaid amount;
3. round using the currency precision of the settlement currency;
4. assign the final rounding remainder to the last item in a stable order (for example, sorted by Commission Entry name and child-row name);
5. never allocate more than an item's remaining unpaid balance;
6. store one immutable payout event per settlement item allocation, with the same source voucher/reference row and a distinct event key per settlement item.

This rule must be confirmed with Finance before implementation. If Finance wants FIFO allocation instead of pro-rata, that decision must be made before code is written and applied consistently to all subsequent payouts.

## 5. Supplier payout flow

### Source of truth

A Supplier payout is recognized from a submitted outbound Payment Entry that satisfies all of the following:

- payment_type == "Pay"
- party_type == "Supplier"
- party equals the Supplier on the linked settlement
- company equals the settlement company
- the Payment Entry is submitted and not cancelled
- an exact Payment Entry Reference row has reference_doctype == "Purchase Invoice" and reference_name == settlement.settlement_document
- that Purchase Invoice is the exact Purchase Invoice created by the settlement
- the row has a positive allocated amount.

### Amount rules

- Use the exact Payment Entry Reference allocation against the linked Purchase Invoice, not paid_amount, received_amount, or the aggregate Payment Entry total.
- If the Payment Entry contains allocations against multiple Purchase Invoices, ignore all non-settlement invoices for this commission payout.
- Preserve the allocation amount in Purchase Invoice currency, the payment instrument amount/currency, and the company-currency equivalent separately.
- Use the source voucher's stored exchange-rate and accounting values. Do not look up today's exchange rate to reconstruct a historical payout.
- Confirm the installed ERPNext v15/v16 Payment Entry Reference conversion semantics and GL allocation fields during the compatibility spike. Use ERPNext's posted accounting values rather than inventing a conversion from unrelated fields.

### Partial, full, and cancelled payments

- Every submitted partial Payment Entry creates only the event(s) for its exact applied amount.
- Additional partial Payment Entries create additional events with new idempotency keys.
- When net payout events equal the settlement amount, the settlement becomes Paid (subject to the evidence policy).
- On Payment Entry cancellation, create reversal events for every payout event generated from its exact reference rows. Do not remove the original events.
- If a Payment Entry is cancelled and amended/replaced, the replacement voucher creates new events; the old voucher's events remain reversed in the audit trail.
- A cancellation must not silently reverse an unrelated invoice allocation from the same Payment Entry.

## 6. Employee payroll payout flow

### Payroll lifecycle

Use the exact linked chain:

Commission Settlement → Additional Salary → Salary Slip → Payroll Entry → Payroll Bank Entry / bank evidence

The implementation must establish the actual linkage using the installed HRMS schema, not by matching employee and dates alone.

### Recognition rules

| Payroll document/event | Commission state |
|---|---|
| Additional Salary submitted | Scheduled for payroll; not Paid. |
| Salary Slip generated but draft | Not Paid. |
| Salary Slip submitted | Payroll calculated/accrued; not Paid. |
| Payroll Entry accrual Journal Entry submitted | Payroll liability booked; not Paid. |
| Payroll Bank Entry Journal Entry submitted | Disbursement recorded in the accounting books; not necessarily externally paid. |
| Verified bank disbursement or approved evidence event tied to the employee's payroll amount | Eligible to create the actual Payout Event. |

Frappe HR's official payroll guide explicitly distinguishes the submitted bank Journal Entry from the external bank transfer. Therefore the Phase 3 employee path must not promote a Salary Slip or Payroll Bank Entry to externally Paid without evidence.

### Employee-level matching

- Match the Additional Salary using its ref_doctype == "Commission Settlement" and ref_docname == settlement.name link.
- Match the exact Salary Slip that includes that Additional Salary; verify the link/child-row field in both supported HRMS versions.
- Match the Salary Slip to its Payroll Entry and the relevant payroll bank/payment record.
- Do not infer payment from employee name + payroll period alone.
- Do not allocate a combined payroll bank debit to an employee using the overall Payroll Entry amount. If the bank transfer is a batch, require employee-level bank-file evidence or another reliable per-employee allocation source.
- Do not use the entire Salary Slip net_pay as the commission amount. The commission amount is the linked Additional Salary component amount in salary currency, converted using the settlement's stored FX snapshot. The Salary Slip net pay is only a control for verifying the employee's payroll disbursement.
- A partial payroll transfer must remain partially/unconfirmed unless a reliable employee-specific allocation is available. The MVP should not invent a commission allocation from an aggregate batch amount.

### Evidence levels

Keep the two payroll concepts separate:

- Payroll Bank Entry means the payment is recorded in ERP's books;
- Bank Reconciled (or equivalent verified external evidence) means the actual transfer has been confirmed.

The final status rule—whether the Commission Settlement's headline Paid status requires bank reconciliation or an approved, attached manual confirmation—must be explicitly selected by Finance before implementation. The audit data must preserve the evidence level either way.

## 7. Balance and status calculation

### 7.1 Ledger-first calculation

Payout balances must be derived from submitted/valid payout events and their compensating events. Do not increment a running amount blindly from hooks.

For a Commission Entry or Settlement Item, define:

- net_paid = sum(Payout commission_amount) - sum(Payout Reversal commission_amount)
- net_recovered = sum(Recovery commission_amount) - sum(Recovery Reversal commission_amount)
- net_entitlement = original eligible entitlement + signed return/adjustment entries
- unpaid = max(0, net_entitlement - net_paid - active_reservations_not_yet_paid)
- recoverable = max(0, net_paid - net_entitlement)

The exact scope of net_entitlement must be aligned to the existing Commission Entry event model: returns and cancellation adjustments should be included once, not subtracted twice. A design test must prove this with both unpaid and previously paid commission.

At settlement level:

- actual_paid_amount is the sum of net payout events allocated to that settlement
- outstanding_payout_amount = max(0, total_payable - actual_paid_amount)
- recoverable_amount is shown separately and is not hidden as a negative outstanding amount
- payout_status is derived from event totals and evidence level, not manually typed

### 7.2 Existing fields

Before using or changing existing Commission Entry.paid_amount and settled_amount, decide one canonical field. The current schema labels settled_amount as actual payout while both fields are read in balance calculations. Recommended approach:

- make one field the canonical materialized payout total
- retain the other only as a documented backward-compatibility alias, or deprecate it after a controlled migration
- never add both together
- derive/reconcile the materialized field from the payout-event ledger
- preserve old values until the migration has classified them.

The Commission Settlement Item.already_settled_amount currently represents a source-entry level value, not necessarily payments made against this specific settlement. Do not use it as this settlement's actual paid total. Introduce settlement-specific payout totals derived from events.

### 7.3 Statuses

Keep approval/reservation state separate from payout state. Proposed payout_status values:

- Not Paid
- Payroll Accrued (employee route only)
- Disbursement Recorded
- Partially Paid
- Paid
- Recovery Due
- Recovered
- Cancelled (only when cancellation rules are satisfied)

The exact status vocabulary can be simplified during implementation, but Submitted or Approved must never be used as synonyms for Paid.

## 8. Returns, reversals, and recoveries

### Return before payout

A return reduces the commission entitlement. Any unpaid amount is reduced first. No historical payout event exists, so no recovery is created for the unpaid portion.

### Return after partial or full payout

- Keep all original payout events unchanged.
- Record the return/cancellation using the existing Commission Entry return/reversal ledger.
- Recalculate entitlement and compare it with cumulative net payout events.
- Any excess of paid over the revised entitlement becomes recoverable_amount.
- Do not create a negative payout event merely to make the displayed balance look correct.

### Recovery

A recovery is a separate event linked to the Commission Entry and (where applicable) the settlement from which the excess payout originated. Supported recovery channels should be designed explicitly:

- recovered by deduction from a later commission settlement
- refunded by Supplier/Employee and recorded by a qualifying inbound payment
- payroll deduction, if legally and operationally supported and represented by an exact payroll source record.

A recovery event reduces the recoverable balance; it does not alter the historic payout event. Reversing a recovery creates a Recovery Reversal event.

### Cancellation rules

- A settlement with no payout may be cancelled under the existing document-cancellation rules.
- A settlement with active payout events must not be cancelled as if no payment happened.
- The source payment must be reversed first, or a separate recovery/correction workflow must handle the obligation.
- The existing guard that blocks settlement cancellation while its Purchase Invoice has submitted allocations must remain; Phase 3 should extend the decision to the payout-event ledger rather than weaken it.

## 9. Idempotency, locking, and transaction safety

1. Generate deterministic event keys from the source voucher, source child-row name, event type, settlement item, and (for reversals) original event key.
2. Enforce uniqueness in the database; duplicate hook invocation must return the existing event rather than double-pay commission balances.
3. Lock the settlement and affected Commission Entries in a stable sorted order before computing and inserting allocations.
4. Re-read submitted source status and exact reference allocation inside the transaction.
5. Use the same lock-order principles already applied to customer Payment Entry / invoice processing; document and test the final order to avoid introducing a Payment Entry <-> Commission Settlement deadlock.
6. Recompute materialized totals from the event ledger after event insertion/reversal. Do not rely on +=/-= updates that can be replayed twice.
7. Let source voucher cancellation errors propagate. Never swallow a database/lookup error and treat it as zero payout.
8. Make source handlers safe to retry after database deadlock or lock timeout. Idempotency must protect the retry path.

## 10. Permissions and audit controls

- Payout events are read-only to normal users and created only from trusted server-side source handlers.
- Validate company, beneficiary, currency, source document, settlement document, and settlement-item membership at the server.
- Supplier Payment Entry must match the settlement's Supplier and Purchase Invoice.
- Employee payroll evidence must match the settlement's Employee, Additional Salary, Salary Slip, and Payroll Entry.
- Reject cross-company links and mismatched parties.
- Manual payout confirmation (if approved) requires a privileged Accounts role, a reason, evidence attachment/reference, timestamp, and user identity. It must be a separately identifiable event type/evidence level.
- Preserve source document names, child-row references, currency snapshots, dates, and reversal chain for audit.

## 11. Migration and legacy data

Do not infer historical payout merely from Commission Settlement.status, Additional Salary.docstatus, Salary Slip.docstatus, or Commission Entry.paid_amount.

Before enabling Phase 3:

1. Inventory all submitted Commission Settlements and linked Additional Salary/Purchase Invoice documents.
2. For Supplier settlements, inspect submitted Payment Entries and their exact Payment Entry Reference rows against the linked Purchase Invoice.
3. For Employee settlements, trace Additional Salary -> Salary Slip -> Payroll Entry, and locate qualifying payroll payment/bank evidence. A submitted Salary Slip or Payroll Bank Entry alone is not external payment proof.
4. Compare discovered evidence to existing paid_amount / settled_amount values.
5. Backfill payout events only where the source evidence is deterministic and complete.
6. Mark ambiguous historical rows as Legacy — Requires Reconciliation; do not auto-mark them Paid.
7. Produce a dry-run report containing settlement name, beneficiary, amount, currency, candidate source document, confidence/reason, and proposed backfill result.
8. Require Finance sign-off on the dry-run before any data migration is run.
9. Keep a rollback/backup plan. Never mutate historical Commission Entry events to manufacture a payout history.

## 12. Testing requirements

### Supplier integration tests

- submitted Payment Entry Pay / Supplier against the exact settlement Purchase Invoice
- Payment Entry paying multiple Purchase Invoices: only the Commission Settlement invoice allocation is counted
- partial payment followed by a second partial payment
- full payment
- Payment Entry cancellation creates an idempotent payout reversal
- amended/replacement Payment Entry creates new events without erasing the old event/reversal chain
- foreign-currency Supplier and company-currency conversions
- duplicate hook invocation
- mismatched supplier, company, invoice, currency, and unrelated Payment Entry Reference are rejected or ignored safely
- concurrent submissions cannot over-allocate the settlement

### Employee/payroll integration tests

- submitting Additional Salary alone creates no payout event
- generating a draft Salary Slip creates no payout event
- submitting a Salary Slip creates no payout event
- submitting the Payroll Entry accrual Journal Entry creates no payout event
- submitting the Payroll Bank Entry creates a recorded-but-not-bank-verified state
- verified per-employee payroll evidence creates the actual payout event
- aggregated payroll payment cannot be allocated to the wrong employee
- Salary Slip without the exact linked Additional Salary does not satisfy the settlement
- duplicate payroll processing is idempotent
- payroll cancellation or bank-entry reversal creates compensating events or is blocked when bank verification has already been recorded

### Return and recovery tests

- return before payout reduces unpaid entitlement but creates no recovery
- return after partial payout creates recovery only for paid amount above revised entitlement
- return after full payout preserves original payout and creates the full applicable recovery
- recovery receipt/deduction reduces recoverable amount
- recovery reversal restores recoverable amount
- subsequent commission offsets recovery without double-counting it
- multiple returns and multiple payouts are applied in deterministic order and currency

### Currency and precision tests

- payment, settlement, commission, and company currencies all equal
- each pair differs in isolation
- all three currencies differ
- source voucher exchange rates are used, not current rates
- rate direction and FX date are retained
- JPY/zero-decimal, YER/three-decimal if configured, and ordinary two-decimal currencies use ERPNext currency precision
- rounding remainder allocation is deterministic and sums exactly to the source allocation

### Regression and operational tests

- customer Receive Payment Entries still update collection eligibility only
- customer receipts never create beneficiary payout events
- return/cancellation and existing accrual-reclassification flows remain correct
- role and company permissions are enforced
- full app tests run under each supported Frappe/ERPNext/HRMS major version in the compatibility matrix

## 13. Implementation work packages (after this design is approved)

### P3.0 — Compatibility and accounting decision gate

- Confirm deployed Frappe, ERPNext, and HRMS versions (v15/v16) and exact relevant DocType fields/hooks.
- Verify Payment Entry Reference allocation currency semantics and the source of company-currency allocated amount.
- Verify Additional Salary -> Salary Slip -> Payroll Entry -> Bank Entry linkage in each HRMS version.
- Decide the evidence policy for employee payroll: bank-reconciled evidence or authorized manual evidence.
- Decide the allocation policy for one payout across multiple Commission Settlement Items (pro-rata recommended, subject to Finance approval).
- Decide canonical paid_amount vs settled_amount field.

Exit: documented decisions, exact source-link map, and approved event schema. No implementation until this gate passes.

### P3.1 — Payout event ledger

- Add the immutable Commission Payout Event DocType and controller.
- Add event-key generation, uniqueness, locking, and event-ledger balance helpers.
- Add read-only audit/report access.

Exit: event ledger tests prove immutability, unique event handling, reversal linkage, and correct currency separation.

### P3.2 — Supplier Payment Entry integration

- Add the outbound Pay / Supplier source handler.
- Match exact Payment Entry Reference rows to the settlement Purchase Invoice.
- Allocate partial/full amounts to settlement items.
- Generate reversal events on source cancellation.

Exit: supplier partial/full/cancel/concurrency/FX tests pass.

### P3.3 — Employee payroll integration

- Add exact Additional Salary and Salary Slip matching.
- Track payroll accrual and bank-entry-recorded states separately from paid.
- Add the approved per-employee external payout confirmation integration.
- Generate immutable payout and reversal events.

Exit: payroll lifecycle tests pass and no Salary Slip/accrual-only path can mark a settlement Paid.

### P3.4 — Return and recovery lifecycle

- Calculate recoverable balances from entitlement and payout events.
- Add recovery and recovery-reversal event sources.
- Apply recovery offsets to future settlement amounts only under an approved policy.

Exit: return-before-pay, return-after-pay, recovery, and reversal tests pass.

### P3.5 — Statuses, UI, reports, and reconciliation

- Add server-derived payout status and totals.
- Separate reserved, payroll-accrued, ERP-recorded, externally confirmed, paid, and recoverable amounts.
- Correct the current client/server outstanding calculation mismatch.
- Provide an event audit trail and a legacy-reconciliation report.

Exit: UI/report totals reconcile exactly to payout events and the General Ledger/source vouchers.

### P3.6 — Migration and release gate

- Run the payout backfill dry run.
- Obtain Finance sign-off.
- Apply migration with backup and reconciliation report.
- Run the full app test suite and version matrix.
- Confirm no source payment can be double-counted and no unverified payroll document is reported as Paid.

Exit: all release criteria below pass.

## 14. Release criteria

Phase 3 is not production-ready until all of the following are true:

- every Paid amount traces to immutable source payout event(s)
- payout event totals reconcile to settlement items and source allocations
- supplier partial payments and cancellations are handled correctly
- employee commission cannot be marked Paid from Additional Salary, Salary Slip, or payroll accrual alone
- external payroll payment evidence is matched at employee level
- return-after-payment creates a recoverable balance without rewriting history
- repeated hooks and concurrent processing cannot duplicate or over-allocate payout
- all currency amounts and rate directions are explicit and auditable
- legacy balances are reconciled or visibly flagged as unresolved
- all tests pass on the supported Frappe/ERPNext/HRMS version matrix
- Finance signs off on allocation policy, evidence policy, recovery policy, and migration results

## 15. Explicit non-goals for this design phase

- Do not modify Python, JavaScript, JSON DocTypes, hooks, reports, or database schema.
- Do not create commits or push changes to GitHub.
- Do not mark historical settlements Paid based on current status labels.
- Do not backfill payout events without exact source evidence.
- Do not change the earning, collection, or accrual calculation engines except where a later approved implementation task explicitly requires a compatibility adjustment.

## Appendix A — Reviewed source locations

All links below are pinned to the reviewed baseline commit:

- Commission Settlement controller
- Commission Settlement DocType
- Commission Entry controller
- Commission Entry DocType
- Commission Collection Event controller
- Commission Collection Event DocType
- Application hooks
- Settlement tests
- Commission Entry tests