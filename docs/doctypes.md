# DocTypes Reference

## Commission Settings (Single)

Global configuration DocType. Controls app-wide settings.

| Field | Type | Notes |
|---|---|---|
| enabled | Check | Global app switch |
| default_company | Link → Company | Default context |
| engine_ownership | Select | Native / Custom / Mixed with guard |
| accounting_mode | Select | Subledger Until Settlement / Periodic Accrual |
| default_commission_currency | Select | Company / Transaction / Plan Currency |
| enable_sales_invoice | Check | Enable invoice source adapter |
| enable_payment_entry | Check | Enable collection source adapter |
| enable_pos_invoice | Check | Enable only after branch-specific POS testing |
| enable_delivery_note | Check | Optional; off by default |
| block_native_hrms_overlap | Check | Enable guarded HRMS overlap validation |
| allow_manual_adjustments | Check | Enable adjustment workflow |
| rounding_policy | Select | Company precision / configured precision |
| default_cost_center | Link → Cost Center | Optional default |
| account_mappings | Table → Commission Account Mapping | Company-specific account mappings |

## Commission Account Mapping (child table)

| Field | Type | Notes |
|---|---|---|
| company | Link → Company | Company scope |
| commission_expense_account | Link → Account | Expense account |
| accrued_commission_payable_account | Link → Account | Liability for accrued commissions |
| payroll_commission_expense_account | Link → Account | Expense account used by payroll component |
| cost_center | Link → Cost Center | Optional dimension |
| default_supplier_payment_term | Link → Payment Terms Template | Optional external settlement default |
| enabled | Check | Active mapping |

## Commission Plan

Top-level document. Holds commission rules, slabs, contributors, and targets.

| Field | Type | Notes |
|---|---|---|
| plan_name | Data (reqd) | Title field |
| enabled | Check (default 1) | Active flag |
| company | Link → Company (reqd) | Company scope |
| valid_from / valid_to | Date | Effective period |
| priority | Int (default 10) | Deterministic plan priority |
| applies_to | Select (reqd) | All / Customer Group / Specific Customer / Territory |
| customer | Link → Customer | Shown when Specific Customer |
| customer_group | Link → Customer Group | Shown when Customer Group |
| territory | Link → Territory | Shown when Territory |
| beneficiary_type | Select | Sales Person / Sales Partner / Both |
| commission_period | Select | Per Invoice / Monthly / Quarterly / Yearly |
| slab_calculation_method | Select | Progressive / Single |
| earning_event | Select | On Invoice Submit / On Delivery / On Payment |
| payout_basis | Select | Accrued / On Collection |
| hold_days | Int | Used only for hold-days policy |
| target_metric | Select | Net Sales / Gross Profit / Quantity / Collected Net Sales |
| stacking_policy | Select | No Stacking / Explicit Stack Groups |
| commission_currency_policy | Select | Company / Transaction / Fixed Currency |
| rules | Table → Commission Rule | Matching rules |
| slabs | Table → Commission Slab | Tiered rates |
| contributors | Table → Commission Contributor | Multi-rep split |
| targets | Table → Commission Target | Target definitions |
| remarks | Small Text | Policy notes |

Permissions: System Manager (full).

## Commission Rule (child table)

| Field | Type | Notes |
|---|---|---|
| rule_code | Data | Stable rule identifier |
| match_type | Select (reqd) | Item / Item Group / All Items |
| item_code | Link → Item | Required when Item |
| item_group | Link → Item Group | Required when Item Group |
| priority | Int (default 10) | Tie-breaking priority |
| basis | Select (reqd) | Net Total / Gross Profit / Fixed Amount |
| calculation_method | Select | Percentage / Fixed Per Unit / Fixed Per Line / Fixed Per Invoice |
| calculation_basis | Select | Net Sales / Gross Profit / Quantity / Fixed |
| rate_percentage | Percent | Applied to basis |
| fixed_amount | Currency | Used for fixed methods |
| fixed_currency | Link → Currency | Fixed commission currency |
| commission_uom | Link → UOM | Unit for fixed-per-unit method |
| use_slabs | Check | Use plan slabs |
| min_commission | Currency | Optional floor |
| max_commission | Currency | Optional cap |
| include_freight | Check | Include configured freight/charges |
| include_free_items | Check | Free/promotional item policy |
| exclude_if_grant_commission_zero | Check (default 1) | Defaults on |
| allow_negative_basis | Check | Defaults off |
| stack_group | Data | Explicitly allowed stacking group |
| enabled | Check (default 1) | Active rule |
| grant_commission | Check (default 1) | Toggle commission on the item |

## Commission Slab (child table)

| Field | Type | Notes |
|---|---|---|
| sequence | Int (default 0) | Sort order |
| slab_name | Data | Label |
| min_attainment | Float (reqd) | Lower threshold |
| max_attainment | Float | Upper threshold (blank = no upper limit) |
| rate_percentage | Percent | Slab rate |
| fixed_bonus | Currency | Optional fixed bonus for crossing threshold |
| enabled | Check (default 1) | Active tier |

## Commission Contributor (child table)

| Field | Type | Notes |
|---|---|---|
| pool_code | Data (default INTERNAL_SALES) | Beneficiary pool |
| contributor_type | Select (reqd) | Sales Person / Sales Partner |
| sales_person | Link → Sales Person | Internal beneficiary |
| sales_partner | Link → Sales Partner | External beneficiary |
| role_type | Select | Hunter / Account Manager / Team Lead / Other |
| split_percentage | Percent (reqd) | Split percentage |
| commission_rate | Percent | Commission rate |
| commission_multiplier | Float (default 1.0) | Optional role multiplier |
| require_employee | Check | Require Employee link before payroll settlement |
| require_supplier | Check | Require Supplier link before supplier settlement |

## Commission Target (child table)

| Field | Type | Notes |
|---|---|---|
| beneficiary_type | Select (reqd) | Sales Person / Sales Partner |
| sales_person | Link → Sales Person | Individual internal target |
| sales_partner | Link → Sales Partner | External partner target |
| item_group | Link → Item Group | Optional product-family target |
| target_metric | Select (reqd) | Net Sales / Gross Profit / Quantity / Collected Net Sales |
| target_value | Float (reqd) | Threshold |
| target_currency | Link → Currency | Required for monetary targets |
| target_uom | Link → UOM | Required for quantity targets |
| period_type | Select (reqd) | Monthly / Quarterly / Yearly |
| monthly_distribution | Link → Monthly Distribution | Optional seasonal distribution |

## Commission Period

| Field | Type | Notes |
|---|---|---|
| company | Link → Company (reqd) | Company scope |
| period_type | Select (reqd) | Monthly / Quarterly / Yearly / Per Invoice |
| period_from / period_to | Date (reqd) | Exact period boundaries |
| fiscal_year | Link → Fiscal Year | ERPNext fiscal-year reference |
| status | Select (read-only) | Open / Closing / Closed / Reopened |
| closed_by / closed_on | Read-only | Close audit |
| reopened_by / reopened_on | Read-only | Reopen audit |
| reopen_reason | Small Text | Mandatory reason for reopening |

## Commission Entry (submittable, immutable)

Generated commission record. All fields read-only. Submitted entries cannot be modified.

| Field | Type | Notes |
|---|---|---|
| event_key | Data (unique) | Idempotency key |
| event_type | Select (reqd) | Invoice Earned / Collection / Return Adjustment / Cancellation Reversal / Manual Adjustment / Settlement Reservation / Settlement Reversal |
| company | Link → Company (reqd) | Company |
| posting_date | Date | Source event date |
| commission_period | Link → Commission Period | Period identity |
| source_doctype | Link → DocType | Source document type |
| source_name | Dynamic Link | Source document |
| source_row | Data | Source child-row name |
| source_payment_entry | Link → Payment Entry | Payment source document |
| source_payment_reference | Data | Payment Entry reference child-row identity |
| original_invoice | Link → Sales Invoice | Original sale |
| return_invoice | Link → Sales Invoice | Return/credit note reference |
| plan | Link → Commission Plan | Plan snapshot reference |
| rule | Link → Commission Rule | Rule snapshot reference |
| pool_code | Data | Beneficiary pool |
| beneficiary_type | Select | Sales Person / Sales Partner |
| sales_person | Link → Sales Person | Internal beneficiary |
| sales_partner | Link → Sales Partner | External beneficiary |
| employee | Link → Employee | Payroll mapping snapshot |
| supplier | Link → Supplier | AP mapping snapshot |
| item_code | Link → Item | Source item |
| item_name | Data | Source item name |
| item_group | Link → Item Group | Source item group snapshot |
| source_qty | Float | Original quantity |
| source_uom | Link → UOM | Transaction UOM |
| commission_qty | Float | Normalized quantity |
| commission_uom | Link → UOM | Commission UOM |
| transaction_currency | Link → Currency | Invoice currency |
| transaction_basis_amount | Currency | Basis in transaction currency |
| company_currency | Link → Currency | Company currency |
| company_basis_amount | Currency | Basis in company currency |
| conversion_rate_snapshot | Float | Original conversion rate |
| commission_currency | Link → Currency | Commission currency |
| commission_rate | Percent | Applied percentage rate |
| fixed_amount_snapshot | Currency | Applied fixed amount |
| earned_delta | Currency | Signed change in earned commission |
| payable_eligible_delta | Currency | Signed change in payable-eligible commission |
| reserved_delta | Currency | Signed change in amount reserved by settlement |
| paid_delta | Currency | Signed change in paid balance |
| reverses_entry | Link → Commission Entry | Original entry being reversed |
| settlement | Link → Commission Settlement | Settlement reference |
| accrual_batch | Link → Commission Accrual Batch | Accrual reference |
| calculation_snapshot | Long Text | Read-only calculation explanation |
| sales_invoice | Link → Sales Invoice | Source invoice |
| commission_basis | Select | Net Total / Gross Profit / Fixed Amount |
| commission_basis_amount | Currency | Basis × split share |
| exchange_rate | Float | Exchange rate |
| earned_commission | Currency | Final commission |
| payable_amount | Currency | Payable amount |
| paid_amount | Currency | Paid amount |
| outstanding_amount | Currency | Outstanding amount |
| status | Select | Accrued / Payable / Partially Paid / Paid / Reversed / Adjusted |
| original_entry | Link → Commission Entry | Original entry (legacy) |
| remarks | Small Text | Explanation |

Permissions: System Manager (full).

## Commission Settlement (submittable)

| Field | Type | Notes |
|---|---|---|
| company | Link → Company (reqd) | Company |
| beneficiary_type | Select (reqd) | Sales Person / Sales Partner |
| sales_person | Link → Sales Person | Internal beneficiary |
| sales_partner | Link → Sales Partner | External beneficiary |
| employee | Link → Employee | Employee settlement mapping |
| supplier | Link → Supplier | Supplier settlement mapping |
| period_from / period_to | Date | Settlement period |
| commission_currency | Link → Currency | Settlement currency |
| settlement_method | Select | Additional Salary / Supplier Purchase Invoice / Manual Approved Route |
| total_earned | Currency (read-only) | Derived amount |
| total_payable | Currency (read-only) | Derived eligible amount |
| total_reserved | Currency (read-only) | Amount reserved |
| total_paid | Currency (read-only) | Derived from linked payment/payroll references |
| outstanding | Currency (read-only) | Derived balance |
| status | Select (read-only) | Draft / Submitted / Approved / Partly Settled / Settled / Cancelled |
| settlement_document_type | Link → DocType | Linked output document type |
| settlement_document | Dynamic Link | Additional Salary, Purchase Invoice, or other approved document |
| entries | Table → Commission Settlement Item | Selected ledger entries |
| approval_user / approval_date | Read-only | Approval audit |
| remarks | Small Text | Comments |

## Commission Settlement Item (child table)

| Field | Type | Notes |
|---|---|---|
| commission_ledger_entry | Link → Commission Entry (reqd) | Ledger entry |
| original_source_invoice | Link → Sales Invoice | Original source invoice |
| beneficiary | Data | Beneficiary |
| earned_balance | Currency (read-only) | Earned balance |
| eligible_balance | Currency (read-only) | Eligible balance |
| reserved_amount | Currency (read-only) | Reserved amount |
| already_settled_amount | Currency (read-only) | Already settled amount |
| amount_included | Currency (reqd) | Amount included in this settlement |
| source_currency_amount | Currency | Source currency amount |
| base_currency_amount | Currency | Base currency amount |

## Commission Accrual Batch (submittable)

| Field | Type | Notes |
|---|---|---|
| company | Link → Company (reqd) | Company |
| period_from / period_to | Date (reqd) | Period |
| accrual_date | Date (reqd) | Accrual date |
| expense_account | Link → Account | Expense account |
| accrued_liability_account | Link → Account | Accrued liability account |
| cost_center | Link → Cost Center | Cost center |
| total_amount | Currency (read-only) | Total amount |
| status | Select (read-only) | Draft / Submitted / Cancelled |
| journal_entry | Link → Journal Entry (read-only) | Journal Entry link |
| items | Table → Commission Accrual Batch Item | Items |

## Commission Accrual Batch Item (child table)

| Field | Type | Notes |
|---|---|---|
| commission_ledger_entry | Link → Commission Entry (reqd) | Ledger entry |
| accrual_amount | Currency (reqd) | Accrual amount |
| commission_expense_account | Link → Account | Expense account |
| accrued_liability_account | Link → Account | Accrued liability account |
| cost_center | Link → Cost Center | Cost center |
| project | Link → Project | Project |