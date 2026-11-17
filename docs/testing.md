# Testing

## Running tests

```bash
cd /path/to/frappe-bench
bench --site <site> run-tests --app sales_commission_management
```

Run a specific DocType's tests:

```bash
bench --site <site> run-tests --app sales_commission_management --module sales_commission_management.sales_commission_management.doctype.commission_entry.test_commission_entry
```

## Writing tests

Place test files next to the DocType: `test_<doctype>.py` (e.g. `test_commission_plan.py`). Frappe auto-discovers them.

Example skeleton:

```python
import frappe
from frappe.tests.utils import FrappeTestCase


class TestCommissionPlan(FrappeTestCase):
    def test_create_plan(self):
        plan = frappe.get_doc({
            "doctype": "Commission Plan",
            "plan_name": "Test Plan",
            "applies_to": "All",
            "enabled": 1,
            "rules": [{"match_type": "All Items", "basis": "Net Total", "rate_percentage": 5}],
        })
        plan.insert()
        self.assertTrue(plan.name)
```

## Coverage goals

- Unit tests for `match_commission_rule`, `get_applicable_commission_plan`, basis calculation.
- Integration tests: submit a Sales Invoice with a Sales Team and assert `Commission Entry` rows.
- Reversal test: cancel invoice → negative entries created.

## Report tests

Script Reports execute via `frappe.desk.query_report.run`. Smoke-test each report after seeding sample data:

```bash
bench --site <site> run-tests --app sales_commission_management --module sales_commission_management.sales_commission_management.report.commission_register.commission_register
```

Or run the full app suite (includes any report test modules):

```bash
bench --site <site> run-tests --app sales_commission_management
```

When adding a new report, add a `test_<report_name>.py` next to the report folder and assert that `execute(filters)` returns columns and a (possibly empty) data list without raising.

## Linting

```bash
cd /path/to/sales_commission_management
ruff check .
ruff format .
```

(No ruff config shipped yet; defaults apply.)
" filepath="C:\Users\lenovo\Downloads\Sales Commission Management for ERPNext\sales_commission_management\docs\testing.md"
