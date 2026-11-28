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
        {"fieldname": "event", "label": _("Payout Event"), "fieldtype": "Link",
         "options": "Commission Payout Event", "width": 160},
        {"fieldname": "settlement", "label": _("Settlement"), "fieldtype": "Link",
         "options": "Commission Settlement", "width": 150},
        {"fieldname": "event_type", "label": _("Event Type"), "fieldtype": "Data", "width": 130},
        {"fieldname": "posting_date", "label": _("Posting Date"), "fieldtype": "Date", "width": 110},
        {"fieldname": "source_doctype", "label": _("Source DocType"), "fieldtype": "Data", "width": 130},
        {"fieldname": "source_name", "label": _("Source"), "fieldtype": "Dynamic Link",
         "options": "source_doctype", "width": 150},
        {"fieldname": "settlement_amount", "label": _("Settlement Amount"), "fieldtype": "Currency", "width": 130},
        {"fieldname": "commission_amount", "label": _("Commission Amount"), "fieldtype": "Currency", "width": 130},
        {"fieldname": "evidence_level", "label": _("Evidence Level"), "fieldtype": "Data", "width": 140},
        {"fieldname": "reversal_of", "label": _("Reversal Of"), "fieldtype": "Link",
         "options": "Commission Payout Event", "width": 150},
        {"fieldname": "recovery_of", "label": _("Recovery Of"), "fieldtype": "Link",
         "options": "Commission Payout Event", "width": 150},
        {"fieldname": "remarks", "label": _("Remarks"), "fieldtype": "Data", "width": 200},
    ]


def get_data(filters):
    flt_filters = {"docstatus": 1}
    if filters.get("settlement"):
        flt_filters["settlement"] = filters["settlement"]
    if filters.get("company"):
        # resolve via settlement's company
        pass
    if filters.get("event_type"):
        flt_filters["event_type"] = filters["event_type"]

    rows = frappe.get_list(
        "Commission Payout Event",
        filters=flt_filters,
        fields=[
            "name",
            "settlement",
            "event_type",
            "posting_date",
            "source_doctype",
            "source_name",
            "settlement_amount",
            "commission_amount",
            "evidence_level",
            "reversal_of",
            "recovery_of",
            "remarks",
        ],
        order_by="posting_date desc, name desc",
        limit_page_length=0,
    )

    # Company filter applied via settlement lookup.
    company = filters.get("company")
    if company:
        allowed = set(
            r.name for r in frappe.get_list(
                "Commission Settlement", filters={"company": company}, fields=["name"]
            )
        )
        rows = [r for r in rows if r.settlement in allowed]

    data = []
    for r in rows:
        data.append(
            {
                "event": r.name,
                "settlement": r.settlement,
                "event_type": r.event_type,
                "posting_date": r.posting_date,
                "source_doctype": r.source_doctype,
                "source_name": r.source_name,
                "settlement_amount": flt(r.settlement_amount, 2),
                "commission_amount": flt(r.commission_amount, 2),
                "evidence_level": r.evidence_level,
                "reversal_of": r.reversal_of,
                "recovery_of": r.recovery_of,
                "remarks": r.remarks,
            }
        )
    return data