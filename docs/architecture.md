# Architecture

## Overview

`sales_commission_management` is a Frappe app that plugs into ERPNext's Sales Invoice / POS Invoice / Payment Entry lifecycles via `doc_events` (hooks) without modifying core code. It calculates commissions per invoice line item and contributor, persists them as `Commission Entry` documents, and supports reversal (clawback) on cancellation, returns, and payment allocation. Settlement and accrual documents create HRMS/ERPNext accounting documents.

## Module layout

```
sales_commission_management/
├── sales_commission_management/
│   ├── hooks.py                      # app metadata + doc_events + scheduler
│   ├── modules.txt                   # "Sales Commission Management"
│   ├── __init__.py
│   └── sales_commission_management/   # inner package (import root for code)
│       ├── __init__.py
│       ├── api.py                    # whitelisted API endpoints
│       └── doctype/
│           ├── commission_settings/
│           ├── commission_account_mapping/
│           ├── commission_plan/
│           ├── commission_rule/
│           ├── commission_slab/
│           ├── commission_contributor/
│           ├── commission_target/
│           ├── commission_period/
│           ├── commission_entry/
│           ├── commission_settlement/
│           ├── commission_settlement_item/
│           ├── commission_accrual_batch/
│           ├── commission_accrual_batch_item/
│       ├── report/                     # 20 Script Reports (one folder per report)
│       │   └── <report_name>/
│       │       ├── <report_name>.py    # execute() returning columns/data
│       │       ├── <report_name>.json  # Report metadata (filters, roles, type)
│       │       └── <report_name>.js    # optional client-side formatter
│       └── workspace/
│           └── Sales Commission Management/
│               └── Sales Commission Management.json  # Workspace shortcuts & sidebar
└── docs/
```

## Data flow

1. User creates a **Commission Plan** with rules (+ optional slabs, contributors, targets).
2. Sales Invoice or POS Invoice is submitted.
3. `generate_commission_entries(doc, method)` runs:
   - Native-overlap guard checks for overlapping HRMS commission payouts.
    - Resolve applicable plan via `_resolve_plan(customer, customer_group, territory, company, posting_date)`.
    - Read contributors from `doc.sales_team` plus `doc.sales_partner`.
    - For each invoice item (skipping `grant_commission == 0`):
      - Match a rule via `_match_rule(plan, item_code, item_group)`.
     - Compute basis amount (Net Total / Gross Profit / Fixed Amount) in company currency.
     - Compute commission amount (percentage / fixed per unit / fixed per line / slabs).
     - Apply min/max caps.
     - For each contributor: split by allocated percentage, insert + submit a `Commission Entry`.
4. On cancellation, `reverse_commission_entries` creates negative (clawback) entries.
5. On return invoice, `handle_return_invoice` creates proportional adjustments referencing the original invoice/entry.
6. `Payment Entry` submit/cancel triggers `update_commission_payment_status`, which allocates paid amounts proportionally and updates `paid_amount`, `outstanding_amount`, `status`.
7. **Commission Settlement** documents select ledger entries and create Additional Salary (internal) or Purchase Invoice (external) documents.
8. **Commission Accrual Batch** documents create a balanced Journal Entry for the selected entries.
9. **Commission Period** documents support open/close/reopen with audit fields and mandatory reopen reason.

## Key design decisions

- **No core modification** — all integration via `doc_events` and scheduler hooks.
- **One entry per (invoice item × contributor)** — keeps attribution auditable.
- **Read-only generated fields** — `Commission Entry` fields are read-only; entries are immutable records.
- **Append-only reversal** — negative entries preserve the audit trail rather than deleting.
- **Multi-currency storage** — commission stored in company currency with transaction currency and exchange-rate snapshot preserved.
- **Native-overlap guard** — blocks overlapping native HRMS commission payouts when enabled.

## Extension points

- `Commission Contributor` child table supports per-plan contributor configuration.
- `recompute_period_slabs()` is the daily slab-attainment recalculation hook.
- Add new commission bases by extending the matching logic in `commission_entry`.
- Add new settlement methods in `Commission Settlement`.
