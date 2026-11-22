# P3.4.2 — Commission Recovery Position Engine

## Design Document

### Purpose
Calculate the recoverable balance for a commission entry based on submitted ledger events.
This is the foundation for the recovery obligation and collection workflow (P3.4.3-P3.4.5).

### Function Signature

```python
def get_commission_recovery_position(commission_entry):
    """Calculate the recoverable balance for a commission entry.

    Returns a dict with:
        - original_commission: float (company currency)
        - net_entitlement: float (company currency) — max(0, original + adjustments)
        - net_paid: float (company currency) — Payouts - Payout Reversals
        - net_recovered: float (company currency) — Recoveries - Recovery Reversals
        - outstanding_recovery: float (company currency) — max(0, net_paid - net_entitlement - net_recovered)
        - currency: str — company currency
        - commission_entry: str — the original entry name
        - payout_events: list — Payout event names
        - recovery_events: list — Recovery event names
        - return_adjustments: list — Return Adjustment entry names
        - has_over_recovery: bool — True if net_recovered > outstanding_recovery (error state)
    """
```

### Financial Calculation

```
Net entitlement = Original commission + Submitted return adjustments + Submitted cancellation reversals
Net paid = Payout events - Payout Reversal events
Net recovered = Recovery events - Recovery Reversal events
Outstanding recovery = max(0, Net paid - max(0, Net entitlement) - Net recovered)
```

### Key Invariants

1. **Never calculate recovery due greater than amount previously paid** — `outstanding_recovery <= net_paid` always.
2. **Use original commission currency throughout** — All calculations in company currency using stored FX snapshots, never revalued.
3. **Fail closed** — Any database or document lookup error aborts the calculation.
4. **Lock the original entry** — Prevent concurrent modifications during calculation.
5. **Submitted events only** — Only `docstatus == 1` events are included.
6. **Deterministic ordering** — Payout events ordered by posting_date, then name.

### Data Sources

| Component | Source | Filter |
|---|---|---|
| Original commission | Commission Entry (original) | `name = commission_entry, docstatus = 1` |
| Return adjustments | Commission Entry | `original_entry = commission_entry, event_type = "Return Adjustment", docstatus = 1` |
| Cancellation reversals | Commission Entry | `reverses_entry in (adjustment names), event_type = "Cancellation Reversal", docstatus = 1` |
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
| Over-recovery (error) | `net_recovered > outstanding_recovery` | More recovered than owed — requires investigation |

### Allocation Strategy

When multiple payout events exist, recovery is allocated deterministically:
1. Order payout events by posting_date, then name (ascending).
2. Allocate recovery against payouts in order (oldest first).
3. Each payout's recoverable amount = `min(payout_amount, remaining_recovery_due)`.

### FX Handling

- All amounts are in company currency.
- Payout events store their own `company_amount` (locked at payout time).
- Recovery events store their own `company_amount` (locked at recovery time).
- No revaluation of historical payouts using current exchange rates.

### Error Handling

- `frappe.ValidationError` for invalid commission entry
- `frappe.ValidationError` for missing currency
- `frappe.ValidationError` for over-recovery state (requires manual investigation)
- Database errors propagate (fail closed)

### Dependencies

- `Commission Entry` DocType (original, Return Adjustment, Cancellation Reversal)
- `Commission Payout Event` DocType (Payout, Payout Reversal, Recovery, Recovery Reversal)
- `currency_precision` helper from utils.py
- `_accrued_for_entry` from commission_accrual_batch.py

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