import frappe


def execute():
    """Ensure the commission_attribution_snapshot custom field exists on Sales Invoice."""
    if not frappe.db.exists(
        "Custom Field", {"dt": "Sales Invoice", "fieldname": "commission_attribution_snapshot"}
    ):
        try:
            frappe.get_doc(
                {
                    "doctype": "Custom Field",
                    "dt": "Sales Invoice",
                    "fieldname": "commission_attribution_snapshot",
                    "label": "Commission Attribution Snapshot",
                    "fieldtype": "Long Text",
                    "hidden": 1,
                    "read_only": 1,
                    "no_copy": 1,
                    "print_hide": 1,
                    "description": "Frozen contributor allocation used for payment-time commission generation.",
                }
            ).insert(ignore_permissions=True)
            frappe.db.commit()
        except Exception:
            frappe.log_error(
                "Failed to create commission_attribution_snapshot custom field",
                "Commission Attribution",
            )

    # Backfill contributor_source on existing Commission Plans so the UI reflects
    # the inferred legacy behaviour (do not silently change behaviour).
    try:
        if not frappe.db.has_column("Commission Plan", "contributor_source"):
            return
        all_plans = frappe.get_all("Commission Plan", pluck="name")
        plans = []
        for pname in all_plans:
            try:
                val = frappe.db.get_value("Commission Plan", pname, "contributor_source")
            except Exception:
                val = None
            if not val:
                plans.append(pname)
        for pname in plans:
            has_contribs = frappe.db.exists(
                "Commission Contributor", {"parent": pname, "parenttype": "Commission Plan"}
            )
            frappe.db.set_value(
                "Commission Plan",
                pname,
                "contributor_source",
                "Plan Contributors" if has_contribs else "Sales Invoice Sales Team",
                update_modified=False,
            )
        frappe.db.commit()
    except Exception:
        frappe.log_error(
            "Failed to backfill contributor_source",
            "Commission Attribution",
        )
    try:
        frappe.get_doc(
            {
                "doctype": "Custom Field",
                "dt": "Sales Invoice",
                "fieldname": "commission_attribution_snapshot",
                "label": "Commission Attribution Snapshot",
                "fieldtype": "Long Text",
                "hidden": 1,
                "read_only": 1,
                "no_copy": 1,
                "print_hide": 1,
                "description": "Frozen contributor allocation used for payment-time commission generation.",
            }
        ).insert(ignore_permissions=True)
        frappe.db.commit()
    except Exception:
        frappe.log_error(
            "Failed to create commission_attribution_snapshot custom field",
            "Commission Attribution",
        )