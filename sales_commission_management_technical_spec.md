---
title: "Sales Commission Management for ERPNext"
subtitle: "Technical and Financial Specification | ERPNext / Frappe v15 and v16"
date: "16 November 2026"
author: "Engineering Specification"
lang: en-US
---

**Application:** `sales_commission_management`  
**Specification status:** Engineering baseline for implementation  
**Primary design objective:** Deliver an auditable, multi-tier commission engine without editing ERPNext or Frappe core files.

---

## 1. Executive decision

Build a separate Frappe application named `sales_commission_management`. Use native Frappe DocTypes, document-event hooks, background jobs, and standard ERPNext/HRMS settlement documents. Do not fork ERPNext and do not replace standard controllers.

The custom application should own commission calculation when an active custom plan applies. It must not also allow the same transaction, salesperson, partner, and period to be paid through the native ERPNext/HRMS commission process. ERPNext already calculates Sales Team incentives on supported sales transactions; HRMS documents Sales Commission and Process Sales Commission for employee payouts; and Sales Partner commission is separate from internal Sales Person commission. The custom app must respect those existing workflows rather than duplicate them.

### 1.1 Recommended version-one policy

| Decision | Version-one design |
|---|---|
| Commission source | Submitted Sales Invoice; optional Delivery Note adapter only after separate testing |
| Eligibility | Commission Plan + matching item rule + eligible beneficiary |
| Earning recognition | On invoice submission, unless plan explicitly says collection-based earning |
| Payout eligibility | Separately configurable: on approval, after collection, or after a hold period |
| Currency | Company currency by default; preserve transaction currency and original conversion rate |
| Sales Team | Use native Sales Team only as attribution input; do not treat contribution as a contractual commission rate |
| Sales Partner | Independent external beneficiary pool; do not read partner from Sales Team rows |
| Returns/cancellations | Append compensating ledger entries; never edit or delete submitted commission entries |
| Accounting | Commission subledger first; controlled accrual and settlement batches create accounting documents |
| Internal payout | HRMS Additional Salary / Salary Slip route, with duplicate-payout guard |
| External payout | Supplier Purchase Invoice and Payment Entry route, with supplier mapping required |
| Customization | `doc_events` common to v15/v16; no core controller override |

### 1.2 Important ERPNext baseline

ERPNext's standard Sales Team flow calculates a contribution amount from net total and then applies the Sales Person commission rate. The standard transaction-level incentive does not itself create a payroll component, expense, payable, or payment. HRMS documents an employee Sales Commission payout route using Additional Salary or Payment Entry. Sales Partner commissions are separate from internal Sales Team attribution. These facts create a material double-payment risk if a custom application is added without an ownership policy.

**Required control:** a company/period/beneficiary must have exactly one active payout owner: the native commission workflow or the custom application. For periods owned by the custom app, block or explicitly exclude overlapping native Sales Commission / Process Sales Commission payouts where HRMS is installed. Verify the exact HRMS DocTypes and fields in the deployed v15/v16 branch before adding the optional guard.

---

## 2. Scope and assumptions

### 2.1 In scope

1. Multiple commission plans with date validity, company, customer/customer-group/territory scope, and deterministic priority.
2. Item, Item Group, and All Items rules.
3. Net sales, gross profit, quantity, fixed-per-unit, and fixed-per-line methods.
4. Single-tier and progressive target slabs.
5. Multiple internal Sales Persons and external Sales Partners.
6. Separate internal and external beneficiary pools.
7. Invoice earning, partial collection, returns, cancellations, and manual approved adjustments.
8. YER company-currency calculations with USD/SAR transaction support.
9. Immutable ledger entries with idempotency keys and references to source documents.
10. Employee payroll and external Supplier settlement workflows.
11. Reports, permissions, reconciliation, and automated tests.
12. Separate v15 and v16 compatibility branches.

### 2.2 Explicitly deferred from version one

- Commission on quotations or unsubmitted Sales Orders.
- Commission on recurring invoices or subscription revenue.
- Complex multi-level management overrides beyond configured beneficiary pools.
- Commission on non-ERPNext external ecommerce orders before they become ERPNext sales documents.
- Automated clawback from already-paid payroll without an HR-approved recovery workflow.
- Real-time commission settlement in a foreign currency where the commission contract is not denominated in company currency.
- Any commission calculation based on estimated cost rather than verified posted cost.

### 2.3 Assumptions to confirm during configuration

The application must not hard-code these policy choices:

- Whether commission is earned on invoice submission, delivery, or customer collection.
- Whether unpaid invoices accrue commission but are not yet payable.
- Whether cash discounts, write-offs, freight, and taxes count toward collection or commission basis.
- Whether a negative gross-profit line earns zero commission or creates a negative adjustment.
- Whether Sales Person and Sales Partner commissions may stack on the same sale.
- Whether target slabs are single-rate or progressive.
- Whether returns reverse commission proportionally or use a special return policy.
- Whether the company wants a formal general-ledger accrual before payout.

Default recommendations are stated in this specification; the Commission Plan must store the applicable settings as snapshots on entries.

---

## 3. Native ERPNext integration and coexistence

### 3.1 Standard functions to reuse

| ERPNext / HRMS object | Use in the custom app |
|---|---|
| Sales Invoice | Primary source document, invoice date, company, customer, currency, item rows, and return linkage |
| Sales Invoice Item | Item-level net amount, quantity, UOM, item/group, and source-row identity |
| Sales Team child table | Internal salesperson attribution and contribution percentage |
| Sales Person | Internal sales identity and optional employee link |
| Employee | Payroll beneficiary identity |
| Sales Partner | External partner identity and transaction-level partner attribution |
| Supplier | Accounts Payable party for an external partner where the partner is paid as a supplier |
| Payment Entry | Actual amount allocated to a Sales Invoice; collection event source |
| Additional Salary / Salary Slip | Employee commission settlement through HRMS |
| Purchase Invoice / Payment Entry | External agent or partner commission settlement |
| Journal Entry | Optional controlled accrual and reclassification document |

### 3.2 Do not assume standard fields are interchangeable

- A Sales Team row represents an internal Sales Person. It does not carry the transaction's Sales Partner.
- The Sales Invoice's Sales Partner is a separate external attribution.
- Sales Team contribution percentage is an attribution split. It is not necessarily the agreed commission rate.
- Sales Person commission rate is not the same as a plan-specific commission rate.
- Native `grant_commission` is a useful eligibility signal, but a custom plan must still apply its own rule and exclusions.
- Standard transaction commission values are not proof that an expense or payable has been posted.

### 3.3 Ownership and duplicate-payout control

Introduce `Commission Engine Ownership` in Commission Settings, scoped by company:

- `Native ERPNext/HRMS`
- `Custom Commission App`
- `Mixed - explicit plan assignment` (only after the HRMS exclusion guard is implemented and tested)

Recommended production setting for the first rollout: `Custom Commission App` for the selected company and effective date. In this mode:

1. Custom Commission Entries are the authoritative subledger.
2. Native Sales Team values may remain visible for attribution, but must not be paid again through native Sales Commission.
3. The app's HRMS integration must reject a native Sales Commission or Process Sales Commission submission that overlaps a custom-owned beneficiary/date period, if the installed HRMS version exposes the relevant hookable DocTypes.
4. If the HRMS guard cannot be verified on a particular branch, operations must disable the native payout route for that company and period and enforce a month-end reconciliation checklist.
5. A report must compare native incentive totals with custom subledger totals and flag overlap.

---

## 4. Business concepts and lifecycle

### 4.1 Separate the financial states

The application must distinguish the following concepts:

- **Calculated:** a rule has produced a commission amount during evaluation.
- **Earned:** the amount has met the earning event defined by the plan.
- **Payable eligible:** the amount has met the collection, hold-period, approval, or other payout conditions.
- **Approved:** an authorized manager/finance user approved it for settlement.
- **Settled:** it has been transferred into a payroll or supplier document.
- **Paid:** the linked payroll/supplier payment workflow confirms payment.
- **Reversed / adjusted:** a compensating ledger event offsets a prior entry.

Do not use one editable `workflow_state` field on each ledger row to represent all of these concepts. Use immutable event entries and derive balances from the ledger and linked settlement documents.

### 4.2 Earning and payout models

Each Commission Plan must select one `earning_trigger`:

1. `Sales Invoice Submit` - commission is earned when an eligible invoice is submitted.
2. `Delivery Note Submit` - deferred until a separately tested Delivery Note source adapter is enabled.
3. `Customer Collection` - commission is earned only as eligible payment is allocated to the invoice.

Each plan must separately select one `payout_eligibility`:

- `On Earned`.
- `On Collection`.
- `After Hold Days`.
- `After Manual Approval`.
- `After Collection And Approval`.

This separation allows an invoice-earned commission to appear in the liability/earned report while remaining unavailable for payment until the customer pays.

### 4.3 Append-only event ledger

Every economic event creates one or more immutable Commission Ledger Entries:

| Event type | Trigger | Earned delta | Payable-eligible delta |
|---|---|---:|---:|
| Invoice Earned | Eligible Sales Invoice submitted | Positive | According to policy |
| Collection | Eligible payment allocated to invoice | Zero for invoice-earned plans; positive for collection-earned plans | Positive according to policy |
| Return Adjustment | Credit note / return submitted | Negative or recalculated reduction | Negative or reduction |
| Cancellation Reversal | Original source invoice cancelled | Reverse remaining earned amount | Reverse remaining eligible amount |
| Manual Adjustment | Approved adjustment document | Positive or negative | Positive or negative |
| Settlement Reservation | Commission Settlement submitted | Zero | Reduces available balance / reserves entries |
| Settlement Reversal | Settlement cancelled before payment | Zero | Restores unconsumed available balance |

The ledger must support source-event references and a deterministic unique key. A repeat hook call must not create another event for the same source event.

---

## 5. Commission calculation policy

### 5.1 Calculation order

For each submitted source document:

1. Confirm that the source DocType and company are enabled.
2. Confirm that the source document is submitted and not already processed for the same event key.
3. Resolve the applicable Commission Plan using effective dates and precedence.
4. Resolve the beneficiary pools (internal Sales Team, external Sales Partner, and any explicit override pool).
5. Resolve one deterministic rule for each item and pool.
6. Calculate the commission basis in company currency, preserving transaction-currency diagnostics.
7. Determine target attainment and slab rate using the configured period and method.
8. Calculate the gross commission, apply caps/floors, and split among beneficiaries.
9. Round once at the beneficiary-entry level using company currency precision.
10. Persist immutable ledger events with the source snapshot and idempotency key.
11. Update only derived summaries/caches; never rewrite posted ledger history.

### 5.2 Rule matching precedence

Never rely on child-table row order. Use this deterministic order:

1. Exact Item Code rule.
2. Most-specific Item Group rule (deepest matching parent path).
3. All Items rule.
4. If two rules at the same specificity and same priority match, block the transaction or mark the event as an exception; do not silently pick the first row.

The plan-level customer scope is resolved before item rules. Suggested plan precedence:

1. Specific Customer.
2. Customer Group.
3. Territory.
4. Company-wide default.

Within the same scope, higher explicit priority wins. Overlapping effective plans with equal priority must be prevented at validation time.

### 5.3 Calculation methods

| Method | Basis | Formula |
|---|---|---|
| Percent of Net Sales | Eligible line net sales in company currency | `basis × rate / 100` |
| Percent of Gross Profit | Verified gross profit in company currency | `max(0, gross_profit) × rate / 100` by default |
| Fixed per Unit | Eligible quantity converted to configured commission UOM | `eligible_qty × fixed_amount_per_uom` |
| Fixed per Line | One fixed amount for each qualifying line | `fixed_amount` |
| Fixed per Invoice | One fixed amount for one qualifying invoice | `fixed_amount` once per invoice and plan/rule |
| Target Slab | Period attainment | Rate determined by single-tier or progressive method, then applied to configured basis |

A zero percentage is valid for fixed methods. Validation must check the method's required fields rather than requiring `rate_percentage` for every rule.

### 5.4 Net sales basis

The default net sales basis is the sales value after item discounts and any invoice-level discount that reduces sales, excluding tax. Freight, delivery, insurance, and other charges are included only if the plan explicitly enables them.

Use the correct company-currency values from the submitted transaction. The implementation must reconcile the sum of item bases to the final invoice net total after document-level discounts. Do not use Grand Total as a default commission basis because it can include taxes and charges that are not sales revenue.

### 5.5 Gross profit basis

Do not implement gross profit as `valuation_rate * qty` without proving that the value matches the actual posted cost for that voucher/item row.

Required policy:

- Use the actual cost recognized for the relevant stock movement / invoice line, tied to the source row and posted accounting/stock records where possible.
- Reconcile the calculated cost to the relevant COGS impact in the General Ledger.
- Define how Delivery Note-linked invoices, returns, non-stock items, service items, batch/serial items, landed-cost adjustments, and negative stock are treated.
- If actual line-level cost cannot be traced reliably, mark the gross-profit calculation as an exception and do not silently substitute a guessed valuation rate.
- For services, permit a separately configured direct-cost basis only when the cost source is defined and auditable.

Default negative-profit behavior: no positive commission on a negative or zero gross-profit basis. An optional plan policy may create a negative commission adjustment, but this must be explicitly approved and tested.

### 5.6 Quantity and UOM

For fixed-per-unit rules, store the commission UOM and convert quantity using the transaction's saved UOM conversion factor. Store both source quantity/UOM and normalized commission quantity/UOM. Never assume the item's stock UOM is the commercial unit agreed for commission.

### 5.7 Discounts, taxes, free items, and service items

Each rule must configure:

- Whether free/promotional items are eligible.
- Whether zero-priced items are eligible.
- Whether returns reduce commission.
- Whether freight/charges are included.
- Whether invoice-level discount is allocated proportionally to item rows.
- Whether service/third-party pass-through items are excluded.
- Whether `grant_commission = 0` is an unconditional exclusion.

Recommended rule: `grant_commission = 0` always excludes the row unless a privileged explicit override is approved and recorded. This gives finance a consistent control over third-party services and non-commissionable lines.

---

## 6. Beneficiaries and split logic

### 6.1 Beneficiary types

Version one supports:

- `Sales Person` (internal salesperson attribution).
- `Sales Partner` (external reseller / agent).

An internal Sales Person may map to an Employee for payroll. A Sales Partner may map to a Supplier for Accounts Payable. The Sales Person and Sales Partner are different entities and must not be stored in the same Link field.

### 6.2 Separate commission pools

Use a `pool_code` to define independent commission pools, for example:

- `INTERNAL_SALES`
- `EXTERNAL_PARTNER`
- `MANAGEMENT_OVERRIDE`

A transaction may legitimately pay an internal salesperson and an external partner at the same time. Do not force those independent pools to total 100% together. Validate percentages within each pool only.

### 6.3 Split sources

Each beneficiary assignment must declare how the split is obtained:

- `Sales Team Contribution` - use the native Sales Team allocated percentage as the attribution basis.
- `Explicit Plan Split` - use the plan's saved fixed split.
- `Transaction Override` - use a controlled transaction-level override table/field, only if implemented and audited.
- `Equal Split` - divide equally among valid members of the same pool.

Do not call Sales Team contribution a contractual commission split. It is only the source of sales attribution unless the plan explicitly adopts it as the split method.

### 6.4 Validation

- Split percentages must be within 0-100.
- If a pool uses percentage allocation, its members must sum to 100% (within a defined decimal tolerance) unless the plan explicitly permits a partial allocation.
- A row cannot contain both Sales Person and Sales Partner.
- A Sales Person intended for payroll must resolve to an Employee before settlement.
- A Sales Partner intended for supplier payment must resolve to an active Supplier before settlement.
- Beneficiary changes after invoice submission must not rewrite existing ledger entries. Corrections require adjustment events.

---

## 7. Targets and slab calculation

### 7.1 Target dimensions

Targets may be defined by:

- Individual Sales Person or Sales Partner beneficiary. Team-level targets are deferred from version one; see the target schema below.
- Company.
- Fiscal Year / commission period.
- Item Group (including child groups where enabled).
- Quantity, net sales, gross profit, or collected net sales.
- Territory or Customer Group if required.

Target attribution must use the same split rules as commission calculation. Do not aggregate full invoice value to every salesperson if the plan uses Sales Team percentages.

### 7.2 Single-tier versus progressive

The plan must explicitly choose one method.

Example: 0-100,000 YER at 2%; 100,000-200,000 YER at 3%; above 200,000 YER at 4%.

- **Single-tier:** once the beneficiary reaches a tier, the selected tier rate applies to the whole eligible basis for the period (subject to plan definition).
- **Progressive:** each marginal slice receives its own rate. The first 100,000 receives 2%, the next 100,000 receives 3%, and amounts above 200,000 receive 4%.

The system must store the attainment snapshot, tier chosen, and calculation detail used for every entry. A later target change must not silently recalculate submitted historical entries.

### 7.3 Period controls

Support Monthly, Quarterly, and Fiscal Year periods. Use company fiscal-year definitions and explicit time zone. Close periods through a Commission Period Close document or controlled close action. Reopen only with elevated permission and an audit reason.

A scheduled job may refresh draft attainment summaries and detect exceptions. It must not rewrite immutable ledger entries or silently change already approved/paid commissions.

---

## 8. Multi-currency and financial precision

### 8.1 Currency policy

Default commission currency: company currency. For a YER company, calculate and store the primary commission liability in YER even when the invoice is in USD or SAR.

Every entry must preserve:

- Transaction currency.
- Transaction-currency basis amount.
- Company currency.
- Company-currency basis amount.
- Original transaction conversion rate from the submitted source document.
- Commission currency and commission amount.
- Currency precision used at posting.

Do not combine a transaction-currency net amount with a company-currency cost amount. Do not recalculate historic commission using today's exchange rate. The exchange rate is a source-document snapshot.

### 8.2 Collection values

For collection-based eligibility, use the amount actually allocated to the Sales Invoice, converted to company currency using the ERPNext payment/reference accounting values. Exclude unallocated advances and unrelated payment balances. The implementation must be verified against the deployed Payment Entry schema for cross-currency allocations; do not assume all allocation fields are expressed in the same currency.

Payment discounts, write-offs, deductions, bank charges, and settlement differences must be individually configurable. Default recommendation: commission is based on cash/settlement allocated to the invoice; write-offs and discounts do not count as cash collection unless the business policy explicitly includes them.

### 8.3 Collection ratio

For invoice-earned plans with collection-gated payout:

`collection_ratio = min(1, cumulative_eligible_collection_base / eligible_invoice_base)`

`payable_eligible_commission = earned_commission × collection_ratio`

Cap the ratio at 100%. Reverse or reduce eligible collections if a submitted Payment Entry is cancelled or its allocation is reversed. Keep collection events linked to the source Payment Entry and reference row.

For mixed-item returns, adjust the eligible invoice basis first, then calculate collection eligibility against the adjusted balance. Do not use the invoice's original gross total if credit notes have reduced the eligible sale.

### 8.4 Precision

- Use Frappe currency/float utilities and DocField precision, not raw Python float arithmetic for monetary posting.
- Keep intermediate calculation precision sufficient for proportional splits.
- Round at the final beneficiary ledger line using company currency precision.
- Allocate any rounding remainder deterministically to the last beneficiary or a configured rounding line; record the method.
- Tests must cover high-value YER amounts and USD/SAR conversions.

---

## 9. Returns, cancellations, and corrections

### 9.1 Sales Invoice cancellation

On cancellation of an invoice that generated commission:

1. Find the original submitted ledger entries by source document.
2. Calculate the remaining un-reversed balance for each original entry.
3. Create one negative compensating entry per original entry, linked through `reverses_entry`.
4. Use an idempotency key tied to the cancellation event.
5. Reverse any unconsumed settlement reservation.
6. If any amount has already been settled/paid, create a receivable/clawback exception for finance review; do not silently create a negative payroll payment.

### 9.2 Credit note / partial return

A submitted return Sales Invoice must create item-level adjustments where original invoice rows can be matched through the return reference. Return only the commission attributable to the returned quantity/value. If item matching is unavailable, use a documented proportional fallback and flag the entry for review.

A return must never reverse the entire original commission unless the return is total. The adjustment must link to the credit note and original invoice entry.

### 9.3 Manual adjustment

Manual adjustments require:

- Reason code.
- Remarks.
- Source or supporting document.
- Positive/negative amount.
- Approver identity.
- Link to original beneficiary, plan, period, and entry where applicable.

Never edit a submitted Commission Ledger Entry to correct it. Create a new adjustment event.

---

## 10. Accounting and settlement architecture

### 10.1 Subledger versus General Ledger

A submitted Commission Ledger Entry is a subledger record. It is not an accounting posting by itself. The app must make this distinction visible in the UI and reports.

Use a company-level Accounting Mode:

1. `Subledger Until Settlement` - version-one default for initial rollout. Commission is calculated and approved in the subledger; the expense/liability is recognized through the resulting payroll or supplier settlement documents.
2. `Periodic Accrual` - optional month-end accrual using a controlled Commission Accrual Batch and Journal Entry. This requires tested settlement-clearing logic to avoid double-expensing.

Do not create an uncontrolled Journal Entry directly inside the Sales Invoice on_submit hook.

### 10.1.1 Expected accounting entries

All examples below assume the commission amount is 10,000 in company currency and exclude taxes and withholding. Actual account names are selected from the company's account mapping. The commission subledger itself does not post a General Ledger entry.

| Event / accounting mode | Debit | Credit |
|---|---|---|
| External partner settlement - Subledger Until Settlement | Commission Expense 10,000 | Supplier Accounts Payable 10,000 |
| Pay supplier | Supplier Accounts Payable 10,000 | Bank / Cash 10,000 |
| Employee payroll settlement - Subledger Until Settlement | Payroll Commission Expense 10,000 | Payroll Payable 10,000 |
| Pay employee through payroll | Payroll Payable 10,000 | Bank / Cash 10,000 |
| Periodic commission accrual | Commission Expense 10,000 | Accrued Commission Payable 10,000 |
| Supplier Purchase Invoice after accrual - preferred only if the branch supports a verified liability-account debit | Accrued Commission Payable 10,000 | Supplier Accounts Payable 10,000 |
| Supplier invoice fallback reclassification, if the Purchase Invoice must debit expense | Accrued Commission Payable 10,000 | Commission Expense 10,000 |
| Employee payroll reclassification after accrual | Accrued Commission Payable 10,000 | Payroll Commission Expense 10,000 |

The supplier and employee payment rows clear the related payable and do not recognize commission expense again. In the periodic-accrual model, the reclassification transfers the accrued liability to supplier or payroll payable while retaining one net commission expense. Do not post both the preferred supplier route and the fallback reclassification. Configure and test exactly one route for each ERPNext/HRMS branch.

### 10.2 External Sales Partner settlement

Prerequisite: Sales Partner has a linked Supplier and agreed supplier currency/payment terms.

Recommended flow:

1. Approve a Commission Settlement for the partner and period.
2. Create a Purchase Invoice against the linked Supplier.
3. For `Subledger Until Settlement`, debit the configured Commission Expense account and credit Accounts Payable through the Purchase Invoice.
4. For `Periodic Accrual`, clear the prior accrual rather than expensing the commission twice. First verify whether the deployed ERPNext branch permits the configured liability account on the Purchase Invoice item account field. If not, post the Purchase Invoice to the commission expense account and create a controlled reclassification Journal Entry: debit Accrued Commission Payable and credit Commission Expense for the same amount. Test this exact route before enabling it.
5. Link the Purchase Invoice to the Commission Settlement and selected Commission Ledger Entries.
6. Pay the supplier through standard Payment Entry.
7. Derive Paid status from submitted payment allocation, not from a user-editable checkbox.

### 10.3 Internal Sales Person / Employee settlement

Prerequisite: Sales Person is linked to an Employee, and HRMS payroll is installed/configured.

Recommended flow:

1. Approve a Commission Settlement for the Employee and payroll period.
2. Create an Additional Salary using a dedicated Salary Component for sales commission.
3. Include it in the relevant Salary Slip.
4. Link the Salary Slip / Additional Salary references back to the Commission Settlement.
5. Derive paid status from the payroll payment process.

For `Periodic Accrual`, when the Salary Slip posts commission expense again, the app must create a tested reclassification Journal Entry: debit Accrued Commission Payable and credit the commission payroll expense account for the portion already accrued. This prevents the same expense from being recognized twice while transferring the liability into payroll payable. If the account mapping and HRMS hook cannot be proven on a branch, disable Periodic Accrual for employee commissions on that branch and use Subledger Until Settlement until the integration is complete.

Do not assume that linking an Employee automatically makes a Sales Person commission payable correctly. Validate Employee, Salary Component, Salary Structure Assignment, Payroll Settings, company, cost center, and currency.

### 10.4 Commission Accrual Batch

The optional accrual batch must:

- Select company and closed commission period.
- Include only earned, approved, not-yet-accrued amounts.
- Group by company, commission expense account, accrued liability account, cost center, project, and beneficiary dimensions where configured.
- Create one balanced Journal Entry or a controlled set of entries.
- Store entry-level references in the `Commission Accrual Batch Item` child table.
- Be idempotent: resubmitting or rerunning must not duplicate accrual.
- Reverse through a linked reversal document, never by deleting the original Journal Entry.

### 10.5 Accounting invariants

- Commission entry totals must reconcile to settlement amounts.
- Purchase Invoice must clear an existing accrual or recognize expense, never both.
- Employee Salary Slip expense must not duplicate an already-posted commission accrual.
- Payment Entry must clear the payable; it must not create a second commission expense.
- Cancelled settlement documents must restore only the outstanding, unpaid commission balance.
- Any exchange gain/loss from paying a foreign-currency supplier is handled by ERPNext's accounting engine and must not rewrite the original commission basis.

---

## 11. DocType design

All DocTypes below live in the custom application. Child DocTypes are marked as child tables. Use standard Frappe field types, Link fields, Currency fields with explicit currency options, and read-only fields for calculated snapshots.

### 11.1 `Commission Settings` (Single)

| Field | Type | Purpose |
|---|---|---|
| enabled | Check | Global app switch |
| default_company | Link: Company | Default context |
| engine_ownership | Select | Native / Custom / Mixed with guard |
| accounting_mode | Select | Subledger Until Settlement / Periodic Accrual |
| default_commission_currency | Select | Company Currency / Transaction Currency / Plan Currency |
| enable_sales_invoice | Check | Enable invoice source adapter |
| enable_payment_entry | Check | Enable collection source adapter |
| enable_pos_invoice | Check | Enable only after branch-specific POS testing |
| enable_delivery_note | Check | Optional; off by default |
| block_native_hrms_overlap | Check | Enable guarded HRMS overlap validation |
| allow_manual_adjustments | Check | Enable adjustment workflow |
| rounding_policy | Select | Company precision / configured precision |
| default_cost_center | Link: Cost Center | Optional default |

Company-specific account mappings should be maintained in a child table, not as a single global account.

### 11.2 `Commission Account Mapping` (Child)

| Field | Type | Purpose |
|---|---|---|
| company | Link: Company | Company scope |
| commission_expense_account | Link: Account | Expense account |
| accrued_commission_payable_account | Link: Account | Liability for accrued commissions |
| payroll_commission_expense_account | Link: Account | Expense account used by payroll component |
| cost_center | Link: Cost Center | Optional dimension |
| default_supplier_payment_term | Link: Payment Terms Template | Optional external settlement default |
| enabled | Check | Active mapping |

Validate that expense accounts are expense accounts, liability accounts are liability accounts, accounts belong to the selected company, and account currencies are compatible with the configured settlement currency.

### 11.3 `Commission Plan` (Parent DocType)

| Field | Type | Purpose |
|---|---|---|
| plan_name | Data | Human-readable name |
| company | Link: Company | Mandatory company scope |
| enabled | Check | Active flag |
| valid_from / valid_to | Date | Effective period |
| priority | Int | Deterministic plan priority |
| applies_to | Select | All Customers / Customer Group / Specific Customer / Territory |
| customer | Link: Customer | Specific customer scope |
| customer_group | Link: Customer Group | Customer group scope |
| territory | Link: Territory | Territory scope |
| earning_trigger | Select | Invoice Submit / Delivery Note Submit / Customer Collection |
| payout_eligibility | Select | On Earned / On Collection / Hold Days / Approval / Collection And Approval |
| hold_days | Int | Used only for hold-days policy |
| commission_period | Select | Monthly / Quarterly / Fiscal Year / Per Invoice |
| target_metric | Select | Net Sales / Gross Profit / Quantity / Collected Net Sales |
| slab_method | Select | Single-Tier / Progressive |
| stacking_policy | Select | No stacking / Explicit stack groups |
| default_pool_policy | Select | Pool configuration mode |
| commission_currency_policy | Select | Company Currency / Transaction Currency / Fixed Currency |
| rules | Table: Commission Rule | Item and calculation rules |
| slabs | Table: Commission Slab | Period tier schedule |
| beneficiary_rules | Table: Commission Beneficiary Rule | Beneficiary and split policy |
| targets | Table: Commission Target | Target definitions by beneficiary, metric, and period |
| remarks | Small Text | Policy notes |

### 11.4 `Commission Rule` (Child DocType)

| Field | Type | Purpose |
|---|---|---|
| rule_code | Data | Stable rule identifier |
| priority | Int | Tie-breaking priority |
| match_type | Select | Item / Item Group / All Items |
| item_code | Link: Item | Exact item match |
| item_group | Link: Item Group | Group match |
| calculation_method | Select | Percent / Fixed per Unit / Fixed per Line / Fixed per Invoice |
| calculation_basis | Select | Net Sales / Gross Profit / Quantity / Fixed |
| rate_percentage | Percent | Required for Percent method only |
| fixed_amount | Currency | Required for fixed methods |
| fixed_currency | Link: Currency | Fixed commission currency if relevant |
| commission_uom | Link: UOM | Unit for fixed-per-unit method |
| minimum_commission | Currency | Optional floor |
| maximum_commission | Currency | Optional cap |
| include_freight | Check | Include configured freight/charges |
| include_free_items | Check | Free/promotional item policy |
| exclude_if_grant_commission_zero | Check | Defaults on |
| allow_negative_basis | Check | Defaults off |
| stack_group | Data | Explicitly allowed stacking group |
| enabled | Check | Active rule |

Validation requirements:

- Item match requires Item Code; Item Group match requires Item Group; All Items requires neither.
- Percent method requires a rate; fixed methods require a fixed amount and must not be skipped because percentage is zero.
- A rule must not be ambiguous at the same plan, specificity, and priority.
- Currency and UOM fields must be valid for the item/company.

### 11.5 `Commission Slab` (Child DocType)

| Field | Type | Purpose |
|---|---|---|
| sequence | Int | Sort order |
| min_attainment | Float | Lower threshold interpreted using the plan target metric and its currency/UOM |
| max_attainment | Float | Upper threshold; blank means no upper limit |
| rate_percentage | Percent | Rate for the tier |
| fixed_bonus | Currency | Optional fixed bonus for crossing a threshold |
| enabled | Check | Active tier |

For quantity-based targets, use numeric thresholds and persist the target UOM. Validation must prohibit overlapping or missing ranges where the policy expects contiguous bands.

### 11.6 `Commission Target` (Child DocType)

| Field | Type | Purpose |
|---|---|---|
| beneficiary_type | Select | Sales Person / Sales Partner |
| sales_person | Link: Sales Person | Individual internal target |
| sales_partner | Link: Sales Partner | External partner target |
| item_group | Link: Item Group | Optional product-family target |
| target_metric | Select | Net Sales / Gross Profit / Quantity / Collected Net Sales |
| target_value | Float | Threshold in the plan target metric |
| target_currency | Link: Currency | Required for monetary targets |
| target_uom | Link: UOM | Required for quantity targets |
| period_type | Select | Monthly / Quarterly / Fiscal Year |
| monthly_distribution | Link: Monthly Distribution | Optional seasonal distribution |

Validate that exactly one beneficiary field matches `beneficiary_type`, the target metric matches its unit field, and the target definition is unique for its company/plan/beneficiary/period/item-group dimensions. Version one supports individual Sales Person and Sales Partner targets only. Defer team-level targets until a dedicated Commission Team DocType and explicit membership/allocation model are designed; do not credit a full team target to every member.

### 11.7 `Commission Beneficiary Rule` (Child DocType)

| Field | Type | Purpose |
|---|---|---|
| pool_code | Data | Internal / partner / override pool |
| beneficiary_type | Select | Sales Person / Sales Partner |
| assignment_source | Select | Sales Team / Transaction Sales Partner / Explicit Plan Split / Equal Split |
| sales_person | Link: Sales Person | Internal beneficiary |
| sales_partner | Link: Sales Partner | External beneficiary |
| split_percentage | Percent | Explicit split where used |
| role_type | Select | Hunter / Account Manager / Team Lead / Other |
| commission_multiplier | Float | Optional role multiplier; default 1.0 |
| require_employee | Check | Require Employee link before payroll settlement |
| require_supplier | Check | Require Supplier link before supplier settlement |

### 11.8 `Commission Ledger Entry` (Immutable DocType)

Use a naming series or generated ID. Make submitted rows immutable through controller validation and permissions.

| Field | Type | Purpose |
|---|---|---|
| event_key | Data, Unique | Idempotency key |
| event_type | Select | Invoice Earned / Collection / Return / Cancellation / Manual Adjustment / Settlement Reservation / Reversal |
| company | Link: Company | Company |
| posting_date | Date | Source event date |
| commission_period | Link: Commission Period | Period identity |
| source_doctype | Link: DocType | Source document type |
| source_name | Dynamic Link | Source document |
| source_row | Data | Source child-row name where relevant |
| source_payment_entry | Link: Payment Entry | Payment source document where relevant |
| source_payment_reference | Data | Payment Entry reference child-row identity where relevant |
| original_invoice | Link: Sales Invoice | Original sale for collection/return events |
| return_invoice | Link: Sales Invoice | Return/credit note reference |
| plan | Link: Commission Plan | Plan snapshot reference |
| rule | Link: Commission Rule | Rule snapshot reference |
| pool_code | Data | Beneficiary pool |
| beneficiary_type | Select | Sales Person / Sales Partner |
| sales_person | Link: Sales Person | Internal beneficiary |
| sales_partner | Link: Sales Partner | External beneficiary |
| employee | Link: Employee | Payroll mapping snapshot |
| supplier | Link: Supplier | AP mapping snapshot |
| item_code | Link: Item | Source item |
| item_group | Link: Item Group | Source item group snapshot |
| source_qty | Float | Original quantity |
| source_uom | Link: UOM | Transaction UOM |
| commission_qty | Float | Normalized quantity |
| commission_uom | Link: UOM | Commission UOM |
| transaction_currency | Link: Currency | Invoice currency |
| transaction_basis_amount | Currency | Basis in transaction currency |
| company_currency | Link: Currency | Company currency |
| company_basis_amount | Currency | Basis in company currency |
| conversion_rate_snapshot | Float | Original conversion rate |
| commission_currency | Link: Currency | Commission currency |
| commission_rate | Percent | Applied percentage rate |
| fixed_amount_snapshot | Currency | Applied fixed amount |
| earned_delta | Currency | Signed change in earned commission |
| payable_eligible_delta | Currency | Signed change in payable-eligible commission |
| reserved_delta | Currency | Signed change in amount reserved by settlement |
| paid_delta | Currency | Signed change in paid balance, linked to settlement/payment where required |
| reverses_entry | Link: Commission Ledger Entry | Original entry being reversed |
| settlement | Link: Commission Settlement | Settlement reference, if applicable |
| accrual_batch | Link: Commission Accrual Batch | Accrual reference, if applicable |
| calculation_snapshot | Long Text / JSON | Read-only calculation explanation |
| remarks | Small Text | Explanation |

**Immutable rules:**

- Do not allow ordinary users to update or delete submitted entries.
- Do not create a negative entry by copying a submitted document and editing it.
- Create a new compensating entry linked to the original entry.
- Use a database unique constraint on `event_key`; a pre-insert existence check alone is insufficient under concurrency.
- Use a unique event key with source event, source row, plan, beneficiary pool, beneficiary, and event type.

### 11.9 `Commission Settlement` (Parent DocType)

| Field | Type | Purpose |
|---|---|---|
| company | Link: Company | Company |
| beneficiary_type | Select | Sales Person / Sales Partner |
| sales_person | Link: Sales Person | Internal beneficiary |
| sales_partner | Link: Sales Partner | External beneficiary |
| employee | Link: Employee | Employee settlement mapping |
| supplier | Link: Supplier | Supplier settlement mapping |
| period_from / period_to | Date | Settlement period |
| commission_currency | Link: Currency | Settlement currency |
| settlement_method | Select | Additional Salary / Supplier Purchase Invoice / Manual approved route |
| total_earned | Currency | Derived amount |
| total_payable | Currency | Derived eligible amount |
| total_reserved | Currency | Amount reserved in this settlement |
| total_paid | Currency | Derived from linked payment/payroll references |
| outstanding | Currency | Derived balance |
| status | Select/read-only | Draft / Submitted / Approved / Partly Settled / Settled / Cancelled |
| settlement_document_type | Link: DocType | Linked output document type |
| settlement_document | Dynamic Link | Additional Salary, Purchase Invoice, or other approved document |
| entries | Table: Commission Settlement Item | Selected ledger entries |
| approval_user / approval_date | Read-only | Approval audit |
| remarks | Small Text | Comments |

### 11.10 `Commission Settlement Item` (Child DocType)

Fields: Commission Ledger Entry, original source invoice, beneficiary, earned balance, eligible balance, reserved amount, already settled amount, amount included in this settlement, and source currency/base currency amounts. Validate that the amount included cannot exceed the currently available eligible balance after other submitted settlements.

### 11.11 `Commission Period` (Parent DocType)

| Field | Type | Purpose |
|---|---|---|
| company | Link: Company | Company scope |
| period_type | Select | Monthly / Quarterly / Fiscal Year / Per Invoice |
| period_from / period_to | Date | Exact period boundaries |
| fiscal_year | Link: Fiscal Year | ERPNext fiscal-year reference where applicable |
| status | Select | Open / Closing / Closed / Reopened |
| closed_by / closed_on | Read-only | Close audit |
| reopened_by / reopened_on | Read-only | Reopen audit |
| reopen_reason | Small Text | Mandatory reason for reopening |

Use this DocType to control target attainment, commission settlement windows, and period close. Closing must verify that ledger events and payment allocations are reconciled; reopening must require a privileged role and reason.

### 11.12 `Commission Accrual Batch` (Optional DocType)

Fields: company, period_from, period_to, accrual_date, expense account, accrued liability account, cost center, total amount, status, Journal Entry link, and a child table of `Commission Accrual Batch Item` rows. The batch must be generated from eligible unaccrued entries and be idempotent.

### 11.13 `Commission Accrual Batch Item` (Child DocType)

| Field | Type | Purpose |
|---|---|---|
| commission_ledger_entry | Link: Commission Ledger Entry | Exact earned entry being accrued; unique within a batch and protected against duplicate accrual across batches |
| accrual_amount | Currency | Amount included in this accrual run |
| commission_expense_account | Link: Account | Expense account used for the accrual line |
| accrued_liability_account | Link: Account | Liability account credited by the accrual |
| cost_center | Link: Cost Center | Optional expense allocation |
| project | Link: Project | Optional project allocation |

The parent batch must validate that child totals equal the Journal Entry amount, every selected ledger entry is eligible and unaccrued, and no entry is accrued twice. For partial accruals, persist the remaining unaccrued balance and enforce `accrued_to_date + current_accrual <= eligible_accrual_amount`.

---

## 12. Application structure

```text
apps/sales_commission_management/
└── sales_commission_management/
    ├── hooks.py
    ├── modules.txt
    ├── patches.txt
    ├── config/
    ├── api/
    │   ├── plans.py
    │   ├── calculations.py
    │   ├── settlements.py
    │   └── reports.py
    ├── events/
    │   ├── sales_invoice.py
    │   ├── payment_entry.py
    │   ├── sales_commission.py       # optional HRMS overlap guard
    │   └── salary_slip.py            # optional accrual-clearing hook
    ├── engine/
    │   ├── plan_resolver.py
    │   ├── rule_resolver.py
    │   ├── basis.py
    │   ├── currency.py
    │   ├── slab.py
    │   ├── allocation.py
    │   ├── ledger.py
    │   ├── collections.py
    │   ├── returns.py
    │   └── idempotency.py
    ├── integrations/
    │   ├── erpnext_sales.py
    │   ├── hrms_payroll.py
    │   ├── supplier_payables.py
    │   └── pos.py
    ├── sales_commission_management/doctype/
    │   ├── commission_settings/
    │   ├── commission_account_mapping/
    │   ├── commission_plan/
    │   ├── commission_rule/
    │   ├── commission_slab/
    │   ├── commission_target/
    │   ├── commission_beneficiary_rule/
    │   ├── commission_ledger_entry/
    │   ├── commission_settlement/
    │   ├── commission_settlement_item/
    │   ├── commission_period/
    │   └── commission_accrual_batch/
    ├── reports/
    ├── patches/
    └── tests/
```

Keep calculation functions independent from the Frappe document-event layer. The event layer should normalize the source document into a stable input structure; the engine should calculate without performing hidden database writes; the ledger service should handle idempotency and persistence.

---

## 13. Integration hooks and processing flow

### 13.1 Portable hook baseline

Use `doc_events` for v15/v16 integration. Frappe documents `doc_events` as the mechanism for attaching CRUD event handlers. `extend_doctype_class` is documented as available in v16+, so it must not be a required dependency for the v15 branch.

Illustrative `hooks.py`:

```python
app_name = "sales_commission_management"

# Keep only handlers for DocTypes confirmed to exist in the installed branch.
doc_events = {
    "Sales Invoice": {
        "on_submit": "sales_commission_management.events.sales_invoice.on_submit",
        "on_cancel": "sales_commission_management.events.sales_invoice.on_cancel",
    },
    "Payment Entry": {
        "on_submit": "sales_commission_management.events.payment_entry.on_submit",
        "on_cancel": "sales_commission_management.events.payment_entry.on_cancel",
    },
}
```

Do not copy this hook list unchanged into production. Confirm the exact installed DocTypes, lifecycle semantics, and hook ordering on both branches. Add optional POS Invoice, Delivery Note, Sales Commission, Process Sales Commission, and Salary Slip hooks only after the relevant app and DocType are confirmed.

### 13.2 Source event processing

- `Sales Invoice.on_submit`: synchronously create the initial event/ledger rows inside the same transaction so an invoice cannot commit without its required commission event. Expensive period rollups can be queued after commit.
- `Sales Invoice.on_cancel`: synchronously create reversal events inside the same transaction.
- `Payment Entry.on_submit`: inspect submitted references to Sales Invoice and create collection events only for actual allocated amounts.
- `Payment Entry.on_cancel`: create compensating collection reversals linked to the original payment events.
- `Sales Invoice` return: process as a return adjustment; do not skip it.
- POS: if the deployment uses Sales Invoice with `is_pos`, test that route; if it uses a separate POS Invoice DocType, implement a dedicated adapter. Prevent processing the same payment through both invoice and Payment Entry paths.

### 13.3 Transaction and idempotency requirements

- Never use a background job as the only source of truth for a financial event if an invoice can commit before the event is guaranteed to exist.
- Use synchronous event capture and ledger insert for the core source event.
- Use `enqueue_after_commit=True` for non-critical summaries, dashboards, and period aggregation.
- The ledger event insert must be protected by a unique event key.
- A repeated event must return the existing entry or safely do nothing.
- Do not swallow calculation or persistence errors. If the commission plan is mandatory and calculation fails, fail the source transaction with a clear message. If the plan is optional, create a visible exception record and notify an authorized user; do not silently lose the commission.
- Avoid `ignore_permissions=True` as a blanket pattern. Use a controlled service account / explicit permission checks and retain `created_by` and audit context.

### 13.4 Background jobs

Use scheduled jobs for reconciliation and exception detection, not for rewriting posted history. Examples:

- Daily: find source documents that should have commission events but do not.
- Daily: compare payment allocation events with submitted Payment Entry references.
- Monthly: prepare draft commission-period attainment and settlement summaries.
- Month-end: prepare an accrual batch for finance review.

After changing `scheduler_events`, run `bench migrate` as required by Frappe. All scheduled jobs must be idempotent and safe to rerun.

---

## 14. Calculation service contracts

The following is an implementation contract, not drop-in production code.

```python
class CommissionCalculationInput:
    company: str
    source_doctype: str
    source_name: str
    posting_date: str
    customer: str
    customer_group: str | None
    territory: str | None
    transaction_currency: str
    company_currency: str
    conversion_rate: Decimal
    items: list
    sales_team: list
    sales_partner: str | None
    is_return: bool
    return_against: str | None


def calculate_commission_events(source) -> list[dict]:
    """Pure calculation: resolve plans/rules and return event payloads.

    Must not insert, submit, cancel, or update Frappe documents.
    """
    ...


def persist_commission_events(events: list[dict]) -> list[str]:
    """Persist using unique event keys; return existing/new entry names."""
    ...
```

Monetary calculations should use `Decimal` or Frappe's precision-aware utilities consistently. Convert to the actual Currency field precision only at the final persistence boundary. Do not use binary float multiplication for financial values.

### 14.1 Pseudocode for item calculation

```python
for source_item in normalized_items:
    if source_item.grant_commission is False:
        continue

    rule = resolve_rule(plan, source_item)
    if not rule:
        continue

    basis = calculate_basis(
        rule=rule,
        source_item=source_item,
        company_currency=source.company_currency,
    )

    if rule.calculation_method == "Percent":
        raw_commission = basis.amount * rule.rate_percentage / Decimal("100")
    elif rule.calculation_method == "Fixed per Unit":
        raw_commission = basis.commission_qty * rule.fixed_amount
    elif rule.calculation_method == "Fixed per Line":
        raw_commission = rule.fixed_amount
    elif rule.calculation_method == "Fixed per Invoice":
        # De-duplicate once per invoice/rule, not once per item.
        raw_commission = invoice_level_fixed_amount_once(...)
    else:
        raise UnsupportedCommissionMethod(rule.calculation_method)

    raw_commission = apply_floor_and_cap(raw_commission, rule)
    events.extend(allocate_to_beneficiary_pools(raw_commission, ...))
```

The production implementation must additionally handle target tiers, return adjustments, invoice-level discount allocation, and rule stacking.

---

## 15. Permissions, audit, and controls

### 15.1 Roles

| Role | Permissions |
|---|---|
| Commission User | Read plans and own/authorized beneficiary entries; no approval or settlement |
| Commission Manager | Create/edit draft plans, review calculations, approve adjustments within authority |
| Commission Approver | Approve settlements and period close; cannot modify submitted ledger entries |
| Accounts User | Create/review accrual and supplier settlement documents |
| Payroll User | Create Additional Salary and payroll settlement for employees |
| Commission Auditor | Read-only access to plans, ledger, source links, calculation snapshots, and reconciliation reports |
| System Manager | Configuration and administration; ledger immutability still enforced in controller code |

Apply company-level permissions and user permissions for company, Sales Person, Sales Partner, Employee, and Supplier where appropriate.

### 15.2 Immutable controls

- Submitted Commission Ledger Entry: no edit/delete.
- Submitted Settlement: no alteration of selected entries or amount; cancel/reverse under controlled rules.
- Approved Plan: changes apply prospectively; historical entry snapshots remain unchanged.
- Manual adjustment: reason and approver required.
- Commission period close: reopen requires elevated role and reason.
- Native/custom ownership: overlap prevention and exception report required.
- No direct writes to ERPNext core tables outside standard Frappe document APIs.

### 15.3 Audit snapshot

Every ledger entry must be explainable without rerunning the current plan. Persist rule ID, plan ID, effective dates, rule method, basis amount, source row, split method, beneficiary split, rate/tier, FX rate, precision, calculation output, and source document references.

---

## 16. Reports and workspace

Version one must include:

1. **Commission Ledger:** event-level details with source, item, plan, rule, beneficiary, period, and amount.
2. **Commission Statement by Sales Person:** earned, payable eligible, settled, paid, outstanding, returns, and adjustments.
3. **Commission Statement by Sales Partner:** same metrics plus Supplier mapping and Purchase Invoice/Payment Entry links.
4. **Commission by Item / Item Group:** compare product profitability and commission expense.
5. **Commission by Customer / Territory:** compare account performance.
6. **Target Attainment:** actual achievement, threshold, tier, and gap to next tier.
7. **Collection Eligibility:** invoice amount, eligible collection, collection ratio, and remaining commission eligible for payment.
8. **Return and Clawback:** original commission, return adjustment, amount already paid, and outstanding recovery exception.
9. **Settlement Register:** Payroll, Supplier Purchase Invoice, Payment Entry, status, and outstanding.
10. **Reconciliation Exceptions:** missing events, duplicate key attempts, native/custom overlap, unlinked beneficiaries, unresolved cost basis, and out-of-balance accruals.

Provide filters for Company, date range, commission period, plan, Sales Person, Sales Partner, Employee, Supplier, Customer, Item Group, currency, and status.

---

## 17. Version compatibility and upgrade policy

### 17.1 Branch strategy

Maintain separate release branches:

- `version-15`
- `version-16`

Keep shared calculation and ledger logic in common modules. Isolate version/app-specific adapters for HRMS, POS, Sales Invoice, and Payment Entry where field or lifecycle behavior differs.

### 17.2 Extension strategy

- Use `doc_events` and custom DocTypes as the common integration foundation.
- Do not use `override_doctype_class` for core selling or accounting DocTypes.
- `extend_doctype_class` may be used for optional v16-only convenience extensions, but must never be required for core commission calculation because Frappe documents it as v16+.
- Use fixtures/patches for Custom Fields and metadata. Do not directly alter core ERPNext DocType JSON or manually alter core database tables.
- Keep every migration patch idempotent and test it against a restored production-like backup.

### 17.3 Upgrade acceptance

A release is approved only after:

1. Fresh install test on v15.
2. Fresh install test on v16.
3. Upgrade from prior custom app version on v15.
4. Upgrade from prior custom app version on v16.
5. Migration test with HRMS installed and with HRMS unavailable (optional integration behavior).
6. Existing invoice submission, cancellation, Payment Entry, POS, and payroll workflows remain operational.
7. Commission totals reconcile before and after migration.
8. No custom app code edits ERPNext core files.

---

## 18. Automated test and acceptance matrix

All monetary tests must use realistic company currency precision and include a multi-currency fixture. Test on both version branches.

| ID | Scenario | Expected result |
|---|---|---|
| T01 | Exact Item Code rule and Item Group rule both match | Exact Item Code wins |
| T02 | Two same-specificity rules with equal priority | Validation error / exception; no silent first-row choice |
| T03 | All Items fallback | Applies only when no more-specific rule matches |
| T04 | Fixed-per-unit rule with zero percentage | Commission is calculated from fixed amount; not skipped |
| T05 | Item with `grant_commission = 0` | Excluded by default |
| T06 | Invoice-level discount | Commission basis reconciles to final net sales after allocated discount |
| T07 | Taxes on invoice | Excluded unless explicit plan configuration says otherwise |
| T08 | Non-stock/service item with GP rule and no cost basis | Exception; no guessed COGS |
| T09 | Stock item GP calculation | Cost ties to relevant posted cost records and reconciles with COGS |
| T10 | Zero/negative GP | No positive commission by default |
| T11 | Sales Team 60/40 split | Internal pool allocates 60/40; does not confuse with commission rate |
| T12 | Sales Team plus Sales Partner | Independent pools; no accidental combined 100% validation |
| T13 | Pool allocations total 99% or 101% | Block unless partial allocation is explicitly enabled |
| T14 | Partial customer payment | Payable eligibility increases only by allocated eligible amount |
| T15 | Unallocated customer advance | Does not create invoice collection eligibility |
| T16 | Payment Entry cancellation | Compensating collection reversal; no duplicate event |
| T17 | Foreign currency invoice and YER company currency | Transaction and company-currency bases remain separate and correct |
| T18 | USD invoice paid in YER or SAR | Collection basis uses verified company-currency allocation |
| T19 | Partial return | Only returned item quantity/value commission is reversed |
| T20 | Full return | Remaining commission is fully reversed |
| T21 | Sales Invoice cancellation | One compensating reversal per original entry |
| T22 | Repeated hook / retry | Unique event key prevents duplicate ledger entry |
| T23 | Concurrent duplicate event processing | Database uniqueness prevents double insert |
| T24 | Employee has no Employee link | Settlement blocked with actionable validation |
| T25 | Partner has no Supplier link | Supplier settlement blocked |
| T26 | Additional Salary settlement | Correct Employee, Salary Component, period, and amount linked |
| T27 | Supplier Purchase Invoice settlement | Correct Supplier, expense/accrual account, and settlement references |
| T28 | Payment Entry pays supplier invoice | Paid balance derives from allocation; expense not duplicated |
| T29 | Accrual batch submitted twice | No duplicate Journal Entry / no double accrual |
| T30 | Payroll after periodic accrual | Reclassification clears accrued liability and avoids duplicate commission expense |
| T31 | Native HRMS payout overlaps custom plan | Submission blocked or explicit overlap exception raised |
| T32 | POS invoice | One commission event only, regardless of integrated Payment Entry behavior |
| T33 | Company A and Company B | Plans, accounts, balances, and permissions stay separated |
| T34 | User without approval role | Cannot approve settlement or manual adjustment |
| T35 | Plan edited after invoice submission | Historical ledger snapshot remains unchanged |
| T36 | Commission period closed | New historical entries/adjustments require controlled reopen or adjustment reason |
| T37 | v15 upgrade / migration | No core override; no schema or data loss |
| T38 | v16 upgrade / migration | No core override; optional v16 extension remains non-essential |

### 18.1 Financial reconciliation tests

For every test company:

- Ledger earned total = approved earned report total.
- Payable eligible total = earned less hold/collection/return constraints.
- Settlement reserved total never exceeds available payable balance.
- Paid total never exceeds submitted settlement amount.
- External supplier open payable equals linked Purchase Invoice outstanding.
- Employee commission on Salary Slip equals linked approved settlement amount.
- Periodic accrual Journal Entry debits equal credits.
- Commission expense recognized once, not once at accrual and again at payout.

---

## 19. Implementation phases

### Phase 0 - Discovery and branch verification

- Confirm installed ERPNext, Frappe, HRMS, POS, and POSAwesome versions.
- Inspect the exact v15 and v16 DocType schemas and controllers for Sales Invoice, Payment Entry, Sales Commission, Process Sales Commission, Additional Salary, Salary Slip, and POS source DocTypes.
- Identify which native commission features are currently used and whether any payouts have already been processed.
- Sign off the earning trigger, payout trigger, return policy, slab type, and accounting mode.

**Exit criteria:** signed policy matrix and compatibility notes for both branches.

### Phase 1 - Core ledger and rules

- Create app, settings, plans, rules, slabs, beneficiary rules, and ledger entries.
- Implement plan resolution, rule resolution, net-sales calculation, fixed-per-unit/line calculation, split allocation, idempotency, and invoice cancellation/return events.
- Implement basic ledger and statements.

**Exit criteria:** T01-T23 pass except tests requiring settlement integrations.

### Phase 2 - Collections and settlements

- Add Payment Entry collection events and cancellation reversals.
- Add Commission Settlement and settlement item reservations.
- Integrate employee Additional Salary and external Supplier Purchase Invoice.
- Add native HRMS overlap guard where supported.

**Exit criteria:** T14-T18 and T24-T28, T31 pass.

### Phase 3 - Gross profit, targets, and accounting accrual

- Add traceable gross profit source and reconciliation.
- Add monthly/quarterly/fiscal-year targets and single/progressive slabs.
- Add accrual batch and payroll reclassification only after account mapping is verified.

**Exit criteria:** T09-T10, T29-T30 and all financial reconciliation tests pass.

### Phase 4 - POS, performance, upgrade hardening

- Add tested POS adapter(s) for the deployed POS workflow.
- Add reconciliation jobs, reports, performance indexes, and load testing.
- Run fresh install and upgrade suite on v15 and v16.

**Exit criteria:** all acceptance tests pass on both branches and finance signs off a parallel-run reconciliation.

---

## 20. Deployment checklist

1. Back up the site database and files.
2. Confirm the exact Frappe / ERPNext / HRMS / POS app versions and branch compatibility.
3. Install the custom app on a staging site first.
4. Run `bench --site <site> install-app sales_commission_management`.
5. Run `bench --site <site> migrate` after installing or changing DocTypes, fixtures, patches, or scheduler hooks.
6. Run the app's test suite on the selected branch.
7. Configure Company, Commission Settings, account mappings, Sales Person-to-Employee links, and Sales Partner-to-Supplier links.
8. Create draft plans and test with representative invoices for each item group, UOM, and currency.
9. Compare custom calculations against manual finance worksheets and existing native ERPNext incentive amounts.
10. Configure the native/custom commission ownership policy and verify no duplicate payout path remains active.
11. Run one closed-period parallel test before enabling live settlements.
12. Monitor reconciliation exceptions daily during the first commission period.

Example commands (replace placeholders with the actual bench/site and branch):

```bash
cd /home/<bench-user>/frappe-bench
bench --site <site-name> list-apps
bench --site <site-name> install-app sales_commission_management
bench --site <site-name> migrate
bench --site <site-name> run-tests --app sales_commission_management
```

Do not run installation or migrations directly on production before successful staging validation and a verified backup.

---

## 21. Definition of done

The application is not production-ready merely because the DocTypes save and an invoice hook runs. It is production-ready only when:

- Commission calculations are deterministic and explainable.
- Multi-currency amounts reconcile in transaction and company currency.
- Returns and cancellations create correct compensating events.
- Duplicate events are prevented at database level.
- Collections are based on actual allocated payments, not unallocated advances.
- Beneficiary type determines the correct settlement path.
- Payroll and supplier settlement do not double-recognize commission expense.
- Native ERPNext/HRMS payout overlap is prevented or controlled.
- Finance can reconcile the subledger, accruals, settlements, and payments.
- All automated tests pass on both v15 and v16.
- Upgrade tests prove no core files are modified and app migrations are safe.

**Final engineering recommendation:** implement Phases 0-2 first and run the commission engine in parallel with the existing process for one complete commission period. Do not enable automatic GL accrual, payroll clearing, or live partner payouts until finance has reconciled the parallel-run results and approved the accounting mappings.

---

## 22. Official technical references

The following official documentation informs the integration boundaries. The implementation team must still inspect the exact source code and DocType metadata in the deployed ERPNext/HRMS branch before coding branch-specific adapters.

1. Frappe Framework - Hooks and DocType CRUD events: https://docs.frappe.io/framework/user/en/python-api/hooks
2. Frappe Framework - Background Jobs and Scheduler Events: https://docs.frappe.io/framework/user/en/api/background_jobs
3. ERPNext - Calculate Incentive for Sales Team: https://docs.frappe.io/erpnext/calculate-incentive-for-sales-team
4. ERPNext - Sales Person: https://docs.frappe.io/erpnext/sales-person
5. ERPNext - Sales Partner Commission: https://docs.frappe.io/erpnext/sales-partner
6. ERPNext v14 documentation - Sales Commission Payout: https://docs.frappe.io/erpnext.erpnext.com/erpnext/v14/user/manual/en/human-resources/sales-commission-payout
7. ERPNext v14 documentation - Process Sales Commission: https://docs.frappe.io/erpnext.erpnext.com/erpnext/v14/user/manual/en/human-resources/process-sales-commission
8. ERPNext - Payment Entry source (current development branch; verify target branch before relying on field details): https://github.com/frappe/erpnext/blob/develop/erpnext/accounts/doctype/payment_entry/payment_entry.py
9. ERPNext - Selling Controller source (current development branch; verify target branch before relying on field details): https://github.com/frappe/erpnext/blob/develop/erpnext/controllers/selling_controller.py

