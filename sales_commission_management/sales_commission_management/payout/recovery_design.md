# P3.4.2 — Commission Recovery Position Engine

## Design Document

### Purpose
Calculate the recoverable balance for a commission entry based on
submitted ledger events (original commission, returns, cancellations,
payouts, payout reversals, recoveries, recovery reversals).

### Function Signature

```python
def get_commission_recovery_position(commission_entry):
    """Calculate the recoverable balance for a commission entry.

    Returns a dict with:
        - original_commission: float (company currency)
        - net_entitlement: float (company currency) — max(0, original + adjustments)
        - net_paid: float (company currency) — Payouts - Payout Reversals
        - net_recovered: float (company currency) — Recoveries - Recovery Reversals
        - recovery_due_before_recovery: float — max(0, net_paid - net_entitlement)
        - outstanding_recovery: float — max(0, recovery_due_before_recovery - net_recovered)
        - has_over_recovery: bool — True if net_recovered > recovery_due_before_recovery + tolerance
        - currency: str — company currency
        - commission_entry: str — the original entry name
        - payout_events: list — Payout event names (ordered by posting_date, name)
        - recovery_events: list — Recovery event names
        - return_adjustments: list — Return Adjustment entry names
        - cancellation_reversals: list — all cancellation reversal entry names
        - payout_allocations: list — per-payout allocation detail
    """
```

### Financial Calculation

```
# All amounts in company currency.
# Original entitlement uses commission_to_company_rate snapshot (immutable).
# Payout/recovery events use their stored company_amount (locked at event time).

Net entitlement = Original commission (company) + Submitted return adjustments (company) + Submitted cancellation reversals (company)
                = max(0, sum of signed ledger amounts in company currency)

Net paid = Sum of Payout event company_amount - Sum of Payout Reversal event company_amount

Net recovered = Sum of Recovery event company_amount - Sum of Recovery Reversal event company_amount

Recovery due before recovery = max(0, Net paid - max(0, Net entitlement))

Outstanding recovery = max(0, Recovery due before recovery - Net recovered)

Has over-recovery = Net recovered > Recovery due before recovery + tolerance
```

### Currency Handling (P1 fix)

- **Original entitlement**: Convert `earned_commission` (commission currency) to company
  currency using the original entry's immutable `commission_to_company_rate` snapshot.
  Never use today's exchange rate.

- **Return adjustments**: Convert using the original adjustment entry's
  `commission_to_company_rate` snapshot.

- **Cancellation reversals**: Convert using the original reversal entry's
  `commission_to_company_rate` snapshot (or the original entry's rate).

- **Payout events**: Use stored `company_amount` (locked at payout time).

- **Recovery events**: Use stored `company_amount` (locked at recovery time).

- All final comparisons are in company currency. No mixing of commission
  currency and company currency.

### Cancellation Reversal Sources (P1 fix)

The recovery engine must account for BOTH types of cancellation reversals:

1. **Cancellation reversals of Return Adjustments** — where
   `reverses_entry IN (Return Adjustment names)`.

2. **Cancellation reversals directly referencing the original Commission Entry** —
   where `reverses_entry = original_entry name` (created by
   `reverse_commission_entries()` when the original Sales Invoice is cancelled).

The entitlement calculation uses the **signed sum** of all ledger events:
- Original Commission Entry: positive (earned_commission in company currency)
- Return Adjustment: negative (clawback)
- Cancellation Reversal (of Return Adjustment): positive (restores)
- Cancellation Reversal (of Original Entry): negative (full clawback on invoice cancel)

This preserves the distinction between negative clawbacks and positive
compensating reversals without relying on event_type filtering.

### Data Sources

| Component | Source | Filter |
|---|---|---|
| Original commission | Commission Entry (original) | `name = commission_entry, docstatus = 1` |
| Return adjustments | Commission Entry | `original_entry = commission_entry, event_type = "Return Adjustment", docstatus = 1` |
| Cancellation reversals (of returns) | Commission Entry | `reverses_entry in (adjustment names), event_type = "Cancellation Reversal", docstatus = 1` |
| Cancellation reversals (of original) | Commission Entry | `reverses_entry = commission_entry, event_type = "Cancellation Reversal", docstatus = 1` |
| Payout events | Commission Payout Event | `commission_entry = commission_entry, event_type = "Payout", docstatus = 1` |
| Payout reversals | Commission Payout Event | `commission_entry = commission_entry, event_type = "Payout Reversal", docstatus = 1` |
| Recovery events | Commission Payout Event | `commission_entry = commission_entry, event_type = "Recovery", docstatus = 1` |
| Recovery reversals | Commission Payout Event | `commission_entry = commission_entry, event_type = "Recovery Reversal", docstatus = 1` |

### Recovery Position States

| State | Condition | Meaning |
|---|---|---|
| No recovery due | `outstanding_recovery == 0` | Either no returns exceeded unpaid commission, or all paid amounts recovered |
| Partial recovery | `0 < outstanding_recovery < net_paid` | Some commission paid but not yet recovered |
| Full recovery | `outstanding_recovery == net_paid` | All paid commission needs recovery |
| Over-recovery (error) | `net_recovered > recovery_due_before_recovery + tolerance` | More recovered than owed — raises frappe.ValidationError |

### Over-Recovery Handling (P1 fix)

```
recovery_due_before_recovery = max(0.0, net_paid - max(0.0, net_entitlement))

has_over_recovery = net_recovered > recovery_due_before_recovery + _RETURN_MONEY_TOLERANCE

outstanding_recovery = max(0.0, recovery_due_before_recovery - net_recovered)
```

If `has_over_recovery` is True, raise `frappe.ValidationError` with diagnostic
amounts (net_paid, net_entitlement, net_recovered, recovery_due_before_recovery).
Do not return a boolean state — raise the exception.

### Payout Allocation (P2 improvement)

The return structure includes explicit payout allocation:

```python
{
    "payout_allocations": [
        {
            "payout_event": "CPE-2026-0001",
            "posting_date": "2026-01-15",
            "net_payout": 100.00,
            "recovered": 40.00,
            "recoverable_balance": 60.00,
        }
    ]
}
```

Allocation strategy (oldest first):
1. Order payout events by posting_date, then name (ascending).
2. Allocate recovery against payouts in order (oldest first).
3. Each payout's recoverable amount = `min(payout_amount, remaining_recovery_due)`.

This enables traceable linkage between recovered money and original payouts
for the recovery-event implementation (P3.4.4).

### Shared Locking Protocol (P2 fix)

All ledger writers must acquire the same lock to prevent race conditions:

1. **Lock order**: Always lock the original Commission Entry first
   (`SELECT ... FROM tabCommission Entry WHERE name = %s FOR UPDATE`).

2. **Lock holders**:
   - `get_commission_recovery_position()` — locks before reading
   - `get_or_create_payout_event()` — must also lock the commission entry
     before inserting (currently locks by event_key only)
   - `create_recovery_obligation()` — locks before creating
   - `create_and_submit_recovery_event()` — locks before submitting

3. **Deadlock prevention**: All writers follow the same lock order:
   Commission Entry → Commission Payout Event. Never lock in reverse order.

4. **Timeout**: Use `innodb_lock_wait_timeout` (default 50s). On timeout,
   retry once, then fail closed.

### FX Handling

- All amounts are in company currency.
- Payout events store their own `company_amount` (locked at payout time).
- Recovery events store their own `company_amount` (locked at recovery time).
- Original entitlement converted using `commission_to_company_rate` (immutable snapshot).
- No revaluation of historical payouts using current exchange rates.

### Error Handling

- `frappe.ValidationError` for invalid commission entry
- `frappe.ValidationError` for missing currency
- `frappe.ValidationError` for over-recovery state (with diagnostic amounts)
- Database errors propagate (fail closed)

### Dependencies

- `Commission Entry` DocType (original, Return Adjustment, Cancellation Reversal)
- `Commission Payout Event` DocType (Payout, Payout Reversal, Recovery, Recovery Reversal)
- `currency_precision` helper from utils.py
- `_accrued_for_entry` from commission_accrual_batch.py
- `_get_return_adjustment_remaining_balances` from commission_entry.py

### Implementation Order

1. Create `payout/recovery.py` with `get_commission_recovery_position()`
2. Add unit tests with mocked document objects
3. Add integration tests with real Frappe fixtures
4. Wire into recovery obligation creation (P3.4.3)
5. Wire into recovery collection (P3.4.4)

### Commit Sequence

1. `feat: P3.4.2 - add commission recovery position engine`
2. `test: P3.4.2 - add recovery position unit and integration tests`
3. `feat: P3.4.3 - add commission recovery obligation ledger`
4. `feat: P3.4.4 - link recovery events to original payouts`
5. `feat: P3.4.5 - implement supplier and employee recovery sources`
6. `feat: P3.4.6 - add recovery accounting and reporting`
7. `test: P3.4.7 - add recovery lifecycle integration and concurrency tests`

### Note on P3.4.1 Status

P3.4.1 is implementation-complete (commit `ec4b8f0`) but awaiting integration-test sign-off.
P3.4.2 implementation should wait until P3.4.1 integration tests pass.
This document covers the design and preparation only.