# Copyright (c) 2026, ECOSIRE and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.utils import flt, getdate


def execute(filters=None):
    filters = filters or {}
    columns = get_columns()
    data = get_data(filters)
    return columns, data


def get_columns():
    return [
        {"fieldname": "posting_date", "label": _("Posting Date"), "fieldtype": "Date", "width": 100},
        {"fieldname": "sales_invoice", "label": _("Sales Invoice"), "fieldtype": "Link", "options": "Sales Invoice", "width": 140},
        {"fieldname": "beneficiary_type", "label": _("Beneficiary Type"), "fieldtype": "Data", "width": 120},
        {"fieldname": "beneficiary", "label": _("Beneficiary"), "fieldtype": "Data", "width": 140},
        {"fieldname": "item_code", "label": _("Item"), "fieldtype": "Link", "options": "Item", "width": 140},
        {"fieldname": "item_group", "label": _("Item Group"), "fieldtype": "Link", "options": "Item Group", "width": 120},
        {"fieldname": "commission_basis", "label": _("Basis"), "fieldtype": "Data", "width": 100},
        {"fieldname": "commission_rate", "label": _("Rate %"), "fieldtype": "Percent", "width": 80},
        {"fieldname": "transaction_basis_amount", "label": _("Basis Amount (Txn)"), "fieldtype": "Currency", "width": 120},
        {"fieldname": "company_basis_amount", "label": _("Basis Amount (Company)"), "fieldtype": "Currency", "width": 130},
        {"fieldname": "earned_commission", "label": _("Commission"), "fieldtype": "Currency", "width": 110},
        {"fieldname": "status", "label": _("Status"), "fieldtype": "Data", "width": 100},
        {"fieldname": "company", "label": _("Company"), "fieldtype": "Link", "options": "Company", "width": 120},
    ]


def get_data(filters):
    flt_filters = {"docstatus": ["!=", 2]}
    if filters.get("company"):
        flt_filters["company"] = filters["company"]
    if filters.get("from_date"):
        flt_filters["posting_date"] = [">=", filters["from_date"]]
    if filters.get("to_date"):
        # combine with from_date if both present
        if "posting_date" in flt_filters and isinstance(flt_filters["posting_date"], list) and flt_filters["posting_date"][0] == ">=":
            flt_filters["posting_date"] = ["between", [filters["from_date"], filters["to_date"]]]
        else:
            flt_filters["posting_date"] = ["<=", filters["to_date"]]
    if filters.get("sales_person"):
        flt_filters["sales_person"] = filters["sales_person"]
    if filters.get("sales_partner"):
        flt_filters["sales_partner"] = filters["sales_partner"]
    if filters.get("item_code"):
        flt_filters["item_code"] = filters["item_code"]
    if filters.get("item_group"):
        flt_filters["item_group"] = filters["item_group"]
    if filters.get("commission_plan"):
        flt_filters["plan"] = filters["commission_plan"]
    if filters.get("status"):
        flt_filters["status"] = filters["status"]

    rows = frappe.get_list(
        "Commission Entry",
        filters=flt_filters,
        fields=["posting_date", "sales_invoice", "beneficiary_type", "sales_person",
                "sales_partner", "item_code", "item_group", "commission_basis",
                "commission_rate", "transaction_basis_amount", "company_basis_amount",
                "earned_commission", "status", "company"],
        order_by="posting_date desc",
        limit_page_length=0,
    )

    data = []
    for r in rows:
        data.append({
            "posting_date": r.posting_date,
            "sales_invoice": r.sales_invoice,
            "beneficiary_type": r.beneficiary_type,
            "beneficiary": r.sales_person or r.sales_partner,
            "item_code": r.item_code,
            "item_group": r.item_group,
            "commission_basis": r.commission_basis,
            "commission_rate": r.commission_rate,
            "transaction_basis_amount": r.transaction_basis_amount,
            "company_basis_amount": r.company_basis_amount,
            "earned_commission": r.earned_commission,
            "status": r.status,
            "company": r.company,
        })
    return data