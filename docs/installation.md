# Installation & Setup

## Prerequisites

- Frappe Framework v15 or v16
- ERPNext v15 or v16 (provides Sales Invoice, Customer, Item, Sales Person, Sales Partner)
- Python >= 3.10

## Install from source

```bash
cd /path/to/frappe-bench
bench get-app /path/to/sales_commission_management
bench --site <site_name> install-app sales_commission_management
bench --site <site_name> migrate
bench build
```

## Install from git (when published)

```bash
bench get-app https://github.com/<org>/sales_commission_management
bench --site <site_name> install-app sales_commission_management
```

## Post-install

1. Log in as Administrator.
2. Open **Commission Plan** (module: Sales Commission Management).
3. Create at least one enabled plan.
4. On a Sales Invoice, populate the **Sales Team** table with Sales Person + allocated %.
5. Submit the invoice – `Commission Entry` records are created automatically.

## Uninstall

```bash
bench --site <site_name> uninstall-app sales_commission_management
bench remove-app sales_commission_management
```

## Development setup

```bash
cd frappe-bench
bench get-app /path/to/sales_commission_management --branch develop
bench --site dev.localhost install-app sales_commission_management
bench --site dev.localhost set-config developer_mode 1
bench start
```
" filepath="C:\Users\lenovo\Downloads\Sales Commission Management for ERPNext\sales_commission_management\docs\installation.md"