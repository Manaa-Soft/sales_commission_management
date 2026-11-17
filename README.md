# Sales Commission Management for ERPNext

Commission engine for ERPNext v15/v16. Calculates sales commission when a Sales Invoice or POS Invoice is submitted. Supports splits between multiple contributors, rules by item or item group, and three commission bases: Net Total, Gross Profit, and Fixed Amount. Handles clawback on cancellation and return, payment collection allocation, settlement runs, and accrual batches.

## Features

- **Commission Plans** — enable/disable per customer, customer group, territory, or global default
- **Commission Rules** — match by Item, Item Group, or All Items; choose basis (Net Total / Gross Profit / Fixed Amount) and rate
- **Commission Slabs** — tiered rate structures (from/to amounts), progressive or single
- **Multi-contributor splits** — Sales Person / Sales Partner with role types (Hunter, Account Manager, Team Lead) and split percentages
- **Automatic generation** — `on_submit` of a Sales Invoice or POS Invoice creates `Commission Entry` records
- **Clawback** — `on_cancel` reverses entries (negative commission); returns create proportional adjustments
- **Payment allocation** — `Payment Entry` submit/cancel updates paid/outstanding amounts on entries
- **Settlement** — settlement runs create Additional Salary (internal) or Purchase Invoice (external) documents
- **Accrual batches** — periodic GL accrual batches with balanced Journal Entries
- **Periods** — open/close/reopen audit controls with mandatory reopen reason
- **Native-overlap guard** — blocks overlapping native HRMS commission payouts when enabled
- **Multi-currency** — company currency storage with transaction currency and exchange-rate snapshot
- **Scheduled recalculation** — daily slab attainment recomputation hook
- **Whitelisted API** — `get_commission_summary` and `get_commission_totals` for reporting
- **Script Reports** — 20 built-in reports (Commission Register, Commission by Item/Plan, Payable Aging, GL Reconciliation, Settlement Register, Target/Slab Achievement, etc.) run from the Report list or Workspace shortcuts
- **Workspace** — "Sales Commission Management" desk workspace with sidebar navigation and shortcuts to 18 of the 20 reports

## DocTypes

| DocType | Type | Purpose |
|---|---|---|
| Commission Plan | Document | Holds rules + slabs; applies to customer/group/territory/all |
| Commission Rule | Child Table | Per-item/group/all matching rule with basis & rate |
| Commission Slab | Child Table | Tiered rate slab (from/to amount) |
| Commission Contributor | Child Table | Multi-rep split (Sales Person/Partner + role + %) |
| Commission Entry | Submittable Document | Generated commission record per contributor per invoice |
| Commission Settings | Single | Engine ownership, accounting mode, native-overlap guard, account mappings |
| Commission Account Mapping | Child Table | Company-level GL account mapping for commission accruals |
| Commission Target | Document | Beneficiary target (net sales / gross profit / collected / quantity) |
| Commission Period | Document | Commission period with close/reopen audit controls |
| Commission Settlement | Submittable Document | Settlement run creating Additional Salary or Purchase Invoice |
| Commission Settlement Item | Child Table | Reserved/paid balances per ledger entry in a settlement |
| Commission Accrual Batch | Submittable Document | Periodic GL accrual batch with Journal Entry |
| Commission Accrual Batch Item | Child Table | Ledger entries included in an accrual batch |

## Installation

```bash
cd frappe-bench
bench get-app https://github.com/<org>/sales_commission_management
bench --site <site> install-app sales_commission_management
bench --site <site> migrate
```

Or install from a local path:

```bash
bench get-app /path/to/sales_commission_management
bench --site <site> install-app sales_commission_management
```

## Configuration

1. Open **Commission Settings** (single) and configure engine ownership, accounting mode, and account mappings per company.
2. Create a **Commission Plan** (Sales Commission Management module).
3. Set `Applies To` (All / Customer Group / Specific Customer / Territory).
4. Add **Commission Rules** (match type, basis, rate, slabs).
5. Optionally add **Commission Slabs** for tiered rates and **Commission Contributors** for multi-rep splits.
6. On a Sales Invoice, populate the **Sales Team** table (Sales Person + allocated %) and optionally set the Sales Partner — this drives contributor splits.
7. Submit the invoice — commission entries are generated automatically.
8. Create **Commission Periods** and close/reopen them with audit reasons as needed.
9. Run **Commission Settlement** and **Commission Accrual Batch** documents to settle and accrue commissions.

## Hooks

Defined in `sales_commission_management/hooks.py`:

- `doc_events["Sales Invoice"]["on_submit"]` → `generate_commission_entries`
- `doc_events["Sales Invoice"]["on_cancel"]` → `reverse_commission_entries`
- `doc_events["POS Invoice"]["on_submit"]` → `generate_commission_entries`
- `doc_events["POS Invoice"]["on_cancel"]` → `reverse_commission_entries`
- `doc_events["Payment Entry"]["on_submit"]` → `update_commission_payment_status`
- `doc_events["Payment Entry"]["on_cancel"]` → `update_commission_payment_status`
- `scheduler_events["daily"]` → `recompute_period_slabs`

## API

```python
frappe.call({
  method: "sales_commission_management.sales_commission_management.api.get_commission_summary",
  args: { sales_person: "SP-001", from_date: "2026-01-01", to_date: "2026-01-31" }
});

frappe.call({
  method: "sales_commission_management.sales_commission_management.api.get_commission_totals",
  args: { sales_person: "SP-001", from_date: "2026-01-01", to_date: "2026-01-31" }
});
```

## Development

See `docs/` for developer documentation:

- [Architecture](docs/architecture.md)
- [DocTypes](docs/doctypes.md)
- [Commission Calculation](docs/calculation.md)
- [API Reference](docs/api.md)
- [Hooks & Events](docs/hooks.md)
- [Installation & Setup](docs/installation.md)
- [Testing](docs/testing.md)
- [Contributing](docs/contributing.md)

## License

MIT — see `license.txt`.
