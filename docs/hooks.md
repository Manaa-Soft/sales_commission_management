# Hooks & Events

All hooks are declared in `sales_commission_management/hooks.py`.

## Doc events

```python
doc_events = {
    "Sales Invoice": {
        "before_submit": "sales_commission_management.sales_commission_management.doctype.commission_entry.commission_entry.neutralize_native_commission",
        "on_submit": "sales_commission_management.sales_commission_management.doctype.commission_entry.commission_entry.generate_commission_entries",
        "on_cancel": "sales_commission_management.sales_commission_management.doctype.commission_entry.commission_entry.reverse_commission_entries",
        "before_update_after_submit": "sales_commission_management.sales_commission_management.doctype.commission_entry.commission_entry.neutralize_native_commission_on_update",
    },
    "POS Invoice": {
        "on_submit": "sales_commission_management.sales_commission_management.doctype.commission_entry.commission_entry.generate_commission_entries",
        "on_cancel": "sales_commission_management.sales_commission_management.doctype.commission_entry.commission_entry.reverse_commission_entries",
    },
    "Payment Entry": {
        "before_submit": "sales_commission_management.sales_commission_management.doctype.commission_entry.commission_entry.lock_referenced_invoices",
        "on_submit": [
            "sales_commission_management.sales_commission_management.payout.manual_payment.process_payment_entry_for_commission",
        ],
        "before_cancel": [
            "sales_commission_management.sales_commission_management.doctype.commission_entry.commission_entry.lock_referenced_invoices",
            "sales_commission_management.sales_commission_management.payout.manual_payment.reverse_payment_entry_payouts",
        ],
    },
    "Journal Entry": {
        "before_cancel": [
            "sales_commission_management.sales_commission_management.payout.manual_payment.reverse_journal_entry_payouts",
        ],
        "on_submit": [
            "sales_commission_management.sales_commission_management.doctype.commission_entry.commission_entry.generate_commission_on_payment",
            "sales_commission_management.sales_commission_management.doctype.commission_entry.commission_entry.update_commission_payment_status",
            "sales_commission_management.sales_commission_management.payout.supplier_payment.process_supplier_payment_entry",
        ],
        "on_cancel": "sales_commission_management.sales_commission_management.doctype.commission_entry.commission_entry.update_commission_payment_status",
    },
}
```

- **Sales Invoice before_submit** — neutralizes native ERPNext commission fields when the native-overlap guard is enabled.
- **Sales Invoice / POS Invoice on_submit** — generates commission entries.
- **Sales Invoice / POS Invoice on_cancel** — creates clawback (negative) entries.
- **Sales Invoice before_update_after_submit** — neutralizes native ERPNext commission fields on the Sales Team when the native-overlap guard is enabled.
- **Payment Entry before_submit / before_cancel** — acquires sorted invoice locks to prevent deadlocks during concurrent payment processing.
- **Payment Entry on_submit / on_cancel** — allocates paid amounts to commission entries, updates paid/outstanding/status, and records immutable Commission Collection Events.

## Scheduler events

```python
scheduler_events = {
    "daily": [
        "sales_commission_management.sales_commission_management.doctype.commission_entry.commission_entry.recompute_period_slabs"
    ]
}
```

Daily job: aggregates Commission Entry earned amounts per beneficiary within each open Commission Period and updates matching Commission Target attainment percentages. Entries are never mutated.

## Adding new hooks

1. Add the function to the appropriate module.
2. Register it in `hooks.py` under `doc_events` or `scheduler_events`.
3. Run `bench migrate` to pick up changes (hooks are read at startup).
4. Restart bench.

## Notes

- Hooks receive `(doc, method)` arguments from Frappe.
- Keep hook functions idempotent where possible; `generate_commission_entries` checks `is_return` to avoid double-processing.
- Heavy work should be queued via `frappe.enqueue` to avoid blocking the request.
- POS Invoice support requires branch-specific testing before enabling in production.
- Payment Entry allocation is proportional across linked commission entries.
- `lock_referenced_invoices` acquires sorted invoice locks before the Payment Entry row is saved (in `before_submit`/`before_cancel`), preventing deadlocks.
- Commission Collection Events are immutable audit records created during `update_commission_payment_status`.