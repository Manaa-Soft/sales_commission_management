import frappe


def execute():
    """Ensure the commission_attribution_snapshot custom field exists on Sales Invoice.

    Idempotent and fail-closed: a failure to create the field raises so the
    migration does not silently skip the backfill.
    """
    if not frappe.db.exists(
        "Custom Field", {"dt": "Sales Invoice", "fieldname": "commission_attribution_snapshot"}
    ):
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

    # Backfill contributor_source on existing Commission Plans. Infer prior
    # behaviour from existing Commission Contributor rows: plans with
    # contributors should be "Plan Contributors"; others "Sales Invoice Sales Team".
    # Only correct plans where the inferred value differs from the stored value,
    # so an administrator's explicit choice is never overwritten.
    if not frappe.db.has_column("Commission Plan", "contributor_source"):
        return
    all_plans = frappe.get_all("Commission Plan", pluck="name")
    for pname in all_plans:
        has_contribs = frappe.db.exists(
            "Commission Contributor", {"parent": pname, "parenttype": "Commission Plan"}
        )
        inferred = "Plan Contributors" if has_contribs else "Sales Invoice Sales Team"
        current = frappe.db.get_value("Commission Plan", pname, "contributor_source")
        if current != inferred:
            frappe.db.set_value(
                "Commission Plan",
                pname,
                "contributor_source",
                inferred,
                update_modified=False,
            )
    frappe.db.commit()