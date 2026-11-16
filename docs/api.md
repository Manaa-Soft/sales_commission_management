# API Reference

## `get_commission_summary`

Whitelisted GET endpoint returning Commission Entry rows.

**Path:** `sales_commission_management.sales_commission_management.api.get_commission_summary`

**Parameters (all optional):**

| Param | Type | Description |
|---|---|---|
| sales_person | string | Filter by Sales Person name |
| from_date | date | Posting date lower bound |
| to_date | date | Posting date upper bound |
| status | string | Filter by entry status |
| company | string | Filter by company |

**Returns:** list of `Commission Entry` rows with fields:
`name`, `sales_invoice`, `posting_date`, `company`, `beneficiary_type`, `sales_person`, `sales_partner`, `item_code`, `commission_basis`, `commission_basis_amount`, `earned_commission`, `payable_amount`, `paid_amount`, `outstanding_amount`, `status`, `transaction_currency`, `company_currency`, `exchange_rate`.

**Example (client):**

```js
frappe.call({
  method: "sales_commission_management.sales_commission_management.api.get_commission_summary",
  args: { sales_person: "SP-001", from_date: "2026-01-01", to_date: "2026-01-31" },
  callback: (r) => console.log(r.message),
});
```

**Example (server):**

```python
from sales_commission_management.sales_commission_management.api import get_commission_summary
rows = get_commission_summary(sales_person="SP-001", from_date="2026-01-01")
```

## `get_commission_totals`

Whitelisted GET endpoint returning aggregated totals across matching entries.

**Path:** `sales_commission_management.sales_commission_management.api.get_commission_totals`

**Parameters (all optional):**

| Param | Type | Description |
|---|---|---|
| sales_person | string | Filter by Sales Person name |
| from_date | date | Posting date lower bound |
| to_date | date | Posting date upper bound |
| company | string | Filter by company |

**Returns:** object with `earned`, `paid`, `outstanding`, `count`.

**Example (client):**

```js
frappe.call({
  method: "sales_commission_management.sales_commission_management.api.get_commission_totals",
  args: { sales_person: "SP-001", from_date: "2026-01-01", to_date: "2026-01-31" },
  callback: (r) => console.log(r.message),
});
```

## Extending the API

Add new whitelisted functions in `api.py` with `@frappe.whitelist()`. Use `frappe.get_all` / `frappe.db` for reads; wrap writes in permission checks.
