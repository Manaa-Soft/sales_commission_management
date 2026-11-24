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

precision = currency_precision(company_currency)

# Step 1: Normalize each submitted ledger-event amount.
original_company = normalize(original_earned_commission * commission_to_company_rate, precision)
return_adj_company = normalize(sum(return_adjustment amounts), precision)
cancel_rev_company = normalize(sum(cancellation_reversal amounts), precision)

# Step 2: Calculate net entitlement (normalized).
net_entitlement = normalize(max(0, original_company + return_adj_company + cancel_rev_company), precision)

# Step 3: Calculate net paid and net recovered from normalized event amounts.
net_payout_i = normalize(payout_amount_i - payout_reversal_i, precision)  # per payout
net_paid = normalize(sum(net_payout_i), precision)
net_recovered = normalize(sum(recovery_i) - sum(recovery_reversal_i), precision)

# Step 4: Calculate recovery obligation (normalized).
recovery_due_before_recovery = normalize(max(0, net_paid - net_entitlement), precision)
outstanding_recovery = normalize(max(0, recovery_due_before_recovery - net_recovered), precision)
```

### Precision Normalization

All monetary amounts are normalized using `currency_precision()` before comparison
and allocation. Normalization follows this order:

1. Normalize each submitted ledger-event amount to company-currency precision.
2. Calculate the net payout and net recovery for each relevant event group.
3. Normalize each resulting balance.
4. Allocate FIFO, normalizing each allocation and remaining balance after subtraction.
5. Assert the three invariants using those normalized values.

The tolerance `_RETURN_MONEY_TOLERANCE` is defined as protection against
floating-point noise before normalization. After normalization, no additional
currency precision unit is allowed to pass as acceptable over-recovery.

Over-recovery check on normalized values (no tolerance after normalization):
```
if net_recovered > recovery_due_before_recovery:
    raise frappe.ValidationError with diagnostic amounts
```

### Currency Handling

- **Original entitlement**: Convert `earned_commission` (commission currency) to company
  currency using the original entry's immutable `commission_to_company_rate` snapshot.
  Never use today's exchange rate.

- **Return adjustments**: Convert using the original adjustment entry's
  `commission_to_company_rate` snapshot (propagated from original entry).

- **Cancellation reversals**: Convert using the original reversal entry's
  `commission_to_company_rate` snapshot (or the original entry's rate).

- **Payout events**: Use stored `company_amount` (locked at payout time).

- **Recovery events**: Use stored `company_amount` (locked at recovery time).

- All final comparisons are in company currency. No mixing of commission
  currency and company currency.

### Historical FX Audit

Before recovery calculations use historical entries, audit that:
- When `commission_currency == transaction_currency`, the stored
  `commission_to_company_rate` is consistent with `conversion_rate_snapshot`.
- For third-currency commissions, verify the independently resolved historical
  snapshot against available evidence.
- Do not automatically replace questionable historical rates with today's
  exchange rate. Flag for manual review.

### Cancellation Reversal Sources

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
| No recovery obligation | Recovery due before recovery = 0 | No returns exceeded unpaid commission |
| Full recovery required | Outstanding = recovery due before recovery | All recovery obligation is outstanding |
| Partially recovered | 0 < Outstanding < recovery due before recovery | Some amount has been recovered |
| Recovery complete | Outstanding = 0 (obligation previously existed) | All recoverable amount has been recovered |
| Over-recovery (exception) | Net recovered > recovery due before recovery | More recovered than owed — raises frappe.ValidationError |

### Over-Recovery Handling

```
recovery_due_before_recovery = normalize(max(0.0, net_paid - max(0.0, net_entitlement)), precision)

outstanding_recovery = normalize(max(0.0, recovery_due_before_recovery - net_recovered), precision)

If net_recovered > recovery_due_before_recovery:
    raise frappe.ValidationError with diagnostic amounts
```

Over-recovery is an exception, not a returned boolean state.

### Payout Allocation

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

Allocation algorithm (exact, using normalized amounts):
1. Calculate each payout's net amount after its own submitted Payout Reversals:
   `net_payout_i = normalize(payout_amount_i - submitted_payout_reversals_i)`
2. Calculate total recovery obligation before recoveries:
   `recovery_due = normalize(max(0, net_paid - max(0, net_entitlement)))`
3. Allocate recovery_due to payouts oldest first (by posting_date, then name):
   `due_i = min(net_payout_i, remaining_recovery_due)`
   `remaining_recovery_due -= due_i`
4. Allocate net_recovered against those obligations, oldest first:
   `recovered_i = min(due_i, remaining_net_recovered)`
   `remaining_net_recovered -= recovered_i`
5. Calculate each payout's outstanding recovery balance:
   `recoverable_balance_i = due_i - recovered_i`

Invariants (evaluated against normalized amounts):
```
sum(due_i) == recovery_due_before_recovery
sum(recovered_i) == net_recovered
sum(recoverable_balance_i) == outstanding_recovery
```

(The second invariant assumes over-recovery has already been rejected.)

A Recovery Reversal must restore the exact payout allocation of the Recovery
event it reverses. The original Recovery event must retain its allocation
details (preferably in a child table linked to individual payout events).
The reversal restores those stored allocations, not a new FIFO calculation.
Exact linkage belongs in P3.4.4 (recovery_of field on Commission Payout Event).

### Shared Locking Protocol

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