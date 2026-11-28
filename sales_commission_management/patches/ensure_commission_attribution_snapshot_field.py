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

    # Backfill contributor_source on existing Commission Plans EXACTLY ONCE.
    #
    # This is a one-time migration of legacy rows that predate the
    # contributor_source field. It must NEVER overwrite an administrator's
    # explicit choice on a subsequent `bench migrate` (after_migrate /
    # after_install both call this function repeatedly).
    #
    # Marker strategy: a plan is considered "not yet migrated" only when its
    # contributor_source is genuinely unset (NULL / empty). Once a value is
    # present -- whether inferred here or chosen by an administrator -- it is
    # left untouched. This makes the backfill idempotent and preserves
    # explicit configuration across repeated migrations.
    if not frappe.db.has_column("Commission Plan", "contributor_source"):
        return
    all_plans = frappe.get_all("Commission Plan", pluck="name")
    for pname in all_plans:
        current = frappe.db.get_value("Commission Plan", pname, "contributor_source")
        # One-time marker: only infer for plans that have never been assigned
        # a contributor_source. Never overwrite an existing (explicit) value.
        if current not in (None, ""):
            continue
        has_contribs = frappe.db.exists(
            "Commission Contributor", {"parent": pname, "parenttype": "Commission Plan"}
        )
        inferred = "Plan Contributors" if has_contribs else "Sales Invoice Sales Team"
        frappe.db.set_value(
            "Commission Plan",
            pname,
            "contributor_source",
            inferred,
            update_modified=False,
        )
    # Do not commit here; Frappe manages the transaction for patches/migrate.