# Copyright (c) 2026, ECOSIRE and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.utils import flt


def execute(filters=None):
    filters = filters or {}
    return get_columns(), get_data(filters)


def get_columns():
    return [
        {"fieldname": "name", "label": _("Settlement"), "fieldtype": "Link", "options": "Commission Settlement", "width": 140},
        {"fieldname": "company", "label": _("Company"), "fieldtype": "Link", "options": "Company", "width": 120},
        {"fieldname": "beneficiary_type", "label": _("Beneficiary Type"), "fieldtype": "Data", "width": 120},
        {"fieldname": "beneficiary", "label": _("Beneficiary"), "fieldtype": "Data", "width": 140},
        {"fieldname": "period_from", "label": _("Period From"), "fieldtype": "Date", "width": 100},
        {"fieldname": "period_to", "label": _("Period To"), "fieldtype": "Date", "width": 100},
        {"fieldname": "settlement_method", "label": _("Method"), "fieldtype": "Data", "width": 140},
        {"fieldname": "total_payable", "label": _("Total Payable"), "fieldtype": "Currency", "width": 120},
        {"fieldname": "total_paid", "label": _("Total Paid"), "fieldtype": "Currency", "width": 110},
        {"fieldname": "outstanding", "label": _("Outstanding"), "fieldtype": "Currency", "width": 110},
        {"fieldname": "status", "label": _("Status"), "fieldtype": "Data", "width": 100},
        {"fieldname": "settlement_document", "label": _("Document"), "fieldtype": "Dynamic Link", "options": "settlement_document_type", "width": 140},
    ]


def get_data(filters):
    flt_filters = {}
    if filters.get("company"):
        flt_filters["company"] = filters["company"]
    if filters.get("from_date"):
        flt_filters["period_from"] = [">=", filters["from_date"]]
    if filters.get("to_date"):
        if "period_from" in flt_filters and isinstance(flt_filters["period_from"], list):
            flt_filters["period_from"] = ["between", [filters["from_date"], filters["to_date"]]]
        else:
            flt_filters["period_from"] = ["<=", filters["to_date"]]

    rows = frappe.get_list(
        "Commission Settlement",
        filters=flt_filters,
        fields=["name", "company", "beneficiary_type", "sales_person", "sales_partner",
                "period_from", "period_to", "settlement_method", "total_payable",
                "total_paid", "outstanding", "status", "settlement_document_type",
                "settlement_document"],
        order_by="creation desc",
        limit_page_length=0,
    )

    data = []
    for r in rows:
        data.append({
            "name": r.name,
            "company": r.company,
            "beneficiary_type": r.beneficiary_type,
            "beneficiary": r.sales_person or r.sales_partner,
            "period_from": r.period_from,
            "period_to": r.period_to,
            "settlement_method": r.settlement_method,
            "total_payable": flt(r.total_payable, 2),
            "total_paid": flt(r.total_paid, 2),
            "outstanding": flt(r.outstanding, 2),
            "status": r.status,
            "settlement_document_type": r.settlement_document_type,
            "settlement_document": r.settlement_document,
        })
    return data