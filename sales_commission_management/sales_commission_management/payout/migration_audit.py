"""Read-only migration audit for the Commission Payout Event ledger (P3.6.1).

This module performs a non-destructive analysis of existing records and
produces a structured report.  It never modifies the database.

Usage (bench):
    bench --site <site> execute \\
        sales_commission_management.sales_commission_management.payout.migration_audit.run_audit

Usage (API, authorized roles only):
    frappe.call('sales_commission_management...payout.migration_audit.run_audit_api')
"""
import frappe
from frappe import _
from frappe.utils import flt, cint


def _safe(fn, *a, **kw):
    try:
        return fn(*a, **kw), None
    except Exception as e:
        return None, str(e)


_AUTHORIZED_ROLES = {"System Manager", "Accounts Manager"}


def _check_api_authorization():
    if frappe.session.user == "Guest":
        frappe.throw(_("Authentication required."), frappe.PermissionError)
    roles = set(frappe.get_roles(frappe.session.user))
    if not (_AUTHORIZED_ROLES & roles):
        frappe.throw(
            _("Insufficient permissions to run the migration audit."),
            frappe.PermissionError,
        )


def _authorized_companies():
    """Return the list of companies the current user may audit.

    System Managers may audit all companies. Accounts Managers are restricted
    to companies for which they have an explicit User Permission.
    """
    roles = set(frappe.get_roles(frappe.session.user))
    if "System Manager" in roles:
        return None  # None means "all companies"
    # Accounts Manager: restrict to permitted companies.
    companies = frappe.get_all(
        "User Permission",
        filters={"user": frappe.session.user, "allow": "Company"},
        pluck="for_value",
    )
    return companies or []


def _is_finite_positive(val):
    import math
    try:
        v = float(val)
    except (TypeError, ValueError):
        return False
    return math.isfinite(v) and v > 0


def run_audit(companies=None):
    """Run the read-only migration audit and return a structured report dict.

    Args:
        companies: optional list of company names to restrict the audit to.
            When None, all companies are audited (trusted bench execution).
    """
    report = {
        "inspected": {},
        "issues": [],
        "financial_impact": {},
        "proposed_actions": [],
        "manual_review": [],
    }

    # Resolve company filter.
    if companies is None:
        companies = _authorized_companies() if frappe.session.user != "Guest" else None
    # None => all companies; [] => no access.

    sett_filters = {"docstatus": 1}
    if companies:
        sett_filters["company"] = ["in", companies]
    elif companies == []:
        # No authorized companies: return empty report.
        report["summary"] = {"records_inspected": {"settlements": 0, "payout_events": 0, "gl_idempotency": 0},
                            "issues_found": 0, "p1_issues": 0, "p2_issues": 0, "manual_review_count": 0}
        return report

    # 1. Submitted settlements
    settlements = frappe.get_all(
        "Commission Settlement",
        filters=sett_filters,
        fields=[
            "name", "company", "status", "total_payable", "total_paid",
            "outstanding", "payout_status", "actual_paid_amount",
            "outstanding_payout_amount", "recoverable_amount",
            "commission_currency", "beneficiary_type", "sales_person",
            "sales_partner", "employee", "supplier",
        ],
    )
    report["inspected"]["settlements"] = len(settlements)

    # 2. Payout events (filtered by authorized companies via settlement link or company field)
    event_filters = {"docstatus": 1}
    if companies:
        event_filters["company"] = ["in", companies]
    events = frappe.get_all(
        "Commission Payout Event",
        filters=event_filters,
        fields=[
            "name", "event_type", "settlement", "commission_entry",
            "company", "source_doctype", "source_name", "source_row",
            "reversal_of", "recovery_of", "event_key",
            "commission_currency", "commission_amount",
            "company_currency", "company_amount",
            "settlement_currency", "settlement_amount",
            "fx_from_currency", "fx_to_currency", "fx_rate",
            "evidence_level", "posting_date", "beneficiary",
        ],
    )
    report["inspected"]["payout_events"] = len(events)

    event_names = {e.name for e in events}
    event_by_name = {e.name: e for e in events}

    # 3. Duplicate event keys
    key_counts = {}
    for e in events:
        key_counts.setdefault(e.event_key, []).append(e.name)
    for key, names in key_counts.items():
        if len(names) > 1:
            report["issues"].append({
                "type": "duplicate_event_key",
                "severity": "P1",
                "event_key": key,
                "events": names,
                "action": "Investigate and reconcile duplicate payout events.",
            })

    # 4. Missing/invalid source references (required fields)
    for e in events:
        if not e.source_doctype or not e.source_name:
            report["issues"].append({
                "type": "missing_source_reference",
                "severity": "P1",
                "event": e.name,
                "source": "{0}:{1}".format(e.source_doctype, e.source_name),
                "action": "Populate source DocType and source name; verify against linked document.",
            })
            continue
        # Source-row ownership check for Payment Entry events
        if e.source_doctype == "Payment Entry":
            if not e.source_row:
                report["issues"].append({
                    "type": "missing_source_row",
                    "severity": "P2",
                    "event": e.name,
                    "action": "Populate the exact Payment Entry Reference row.",
                })
            else:
                # Verify the child row exists and belongs to the source PE.
                try:
                    row_parent = frappe.db.get_value(
                        "Payment Entry Reference", e.source_row, "parent"
                    )
                    row_ref_dt = frappe.db.get_value(
                        "Payment Entry Reference", e.source_row, "reference_doctype"
                    )
                    row_ref_name = frappe.db.get_value(
                        "Payment Entry Reference", e.source_row, "reference_name"
                    )
                    row_allocated = frappe.db.get_value(
                        "Payment Entry Reference", e.source_row, "allocated_amount"
                    )
                    if row_parent is None:
                        report["issues"].append({
                            "type": "missing_source_row_record",
                            "severity": "P1",
                            "event": e.name,
                            "source_row": e.source_row,
                            "action": "Payment Entry Reference row does not exist.",
                        })
                    elif row_parent != e.source_name:
                        report["issues"].append({
                            "type": "source_row_ownership_mismatch",
                            "severity": "P1",
                            "event": e.name,
                            "source_row": e.source_row,
                            "parent": row_parent,
                            "expected_parent": e.source_name,
                            "action": "Source row must belong to the referenced Payment Entry.",
                        })
                    if row_ref_dt and e.settlement:
                        sett_doc = frappe.db.get_value(
                            "Commission Settlement", e.settlement, "settlement_document"
                        )
                        if sett_doc and row_ref_name != sett_doc:
                            report["issues"].append({
                                "type": "source_row_reference_mismatch",
                                "severity": "P1",
                                "event": e.name,
                                "action": "Source row must reference the settlement's Purchase Invoice.",
                            })
                except Exception as ex:
                    report["issues"].append({
                        "type": "source_row_check_error",
                        "severity": "P2",
                        "event": e.name,
                        "error": str(ex),
                    })
        try:
            if not frappe.db.exists(e.source_doctype, e.source_name):
                report["issues"].append({
                    "type": "missing_source_document",
                    "severity": "P1",
                    "event": e.name,
                    "source": "{0}:{1}".format(e.source_doctype, e.source_name),
                    "action": "Verify source document exists; mark for manual review.",
                })
        except Exception as ex:
            report["issues"].append({
                "type": "source_reference_check_error",
                "severity": "P2",
                "event": e.name,
                "error": str(ex),
            })

    # 5. Payout reversals without valid original event
    for e in events:
        if e.event_type in ("Payout Reversal", "Recovery Reversal"):
            if not e.reversal_of or e.reversal_of not in event_names:
                report["issues"].append({
                    "type": "orphan_reversal",
                    "severity": "P1",
                    "event": e.name,
                    "reversal_of": e.reversal_of,
                    "action": "Link to valid original event or mark for manual review.",
                })
                continue
            orig = event_by_name.get(e.reversal_of)
            if orig:
                # Validate correct event type mapping
                if e.event_type == "Payout Reversal" and orig.event_type != "Payout":
                    report["issues"].append({
                        "type": "invalid_reversal_type",
                        "severity": "P1",
                        "event": e.name,
                        "original": e.reversal_of,
                        "action": "Reversal must reference a Payout event.",
                    })
                if e.event_type == "Recovery Reversal" and orig.event_type != "Recovery":
                    report["issues"].append({
                        "type": "invalid_reversal_type",
                        "severity": "P1",
                        "event": e.name,
                        "original": e.reversal_of,
                        "action": "Reversal must reference a Recovery event.",
                    })
                # Same settlement, company, commission entry
                if e.settlement != orig.settlement:
                    report["issues"].append({
                        "type": "reversal_settlement_mismatch",
                        "severity": "P1",
                        "event": e.name,
                        "action": "Reversal settlement must match original event.",
                    })
                if e.company != orig.company:
                    report["issues"].append({
                        "type": "reversal_company_mismatch",
                        "severity": "P1",
                        "event": e.name,
                        "action": "Reversal company must match original event.",
                    })
                if e.commission_entry != orig.commission_entry:
                    report["issues"].append({
                        "type": "reversal_entry_mismatch",
                        "severity": "P1",
                        "event": e.name,
                        "action": "Reversal commission entry must match original event.",
                    })

    # 6. Recovery events without valid payout reference
    for e in events:
        if e.event_type == "Recovery":
            if not e.recovery_of or e.recovery_of not in event_names:
                report["issues"].append({
                    "type": "orphan_recovery",
                    "severity": "P1",
                    "event": e.name,
                    "recovery_of": e.recovery_of,
                    "action": "Link to valid payout event or mark for manual review.",
                })
                continue
            orig = event_by_name.get(e.recovery_of)
            if orig and orig.event_type != "Payout":
                report["issues"].append({
                    "type": "invalid_recovery_reference",
                    "severity": "P1",
                    "event": e.name,
                    "recovery_of": e.recovery_of,
                    "action": "Recovery must reference a Payout event.",
                })
            if orig and e.company != orig.company:
                report["issues"].append({
                    "type": "recovery_company_mismatch",
                    "severity": "P1",
                    "event": e.name,
                    "action": "Recovery company must match original payout.",
                })
            if orig and e.commission_entry != orig.commission_entry:
                report["issues"].append({
                    "type": "recovery_entry_mismatch",
                    "severity": "P1",
                    "event": e.name,
                    "action": "Recovery commission entry must match original payout.",
                })
            if orig and e.settlement != orig.settlement:
                report["issues"].append({
                    "type": "recovery_settlement_mismatch",
                    "severity": "P1",
                    "event": e.name,
                    "action": "Recovery settlement must match original payout.",
                })

    # 7. FX and currency validation
    for e in events:
        currencies = [e.commission_currency, e.company_currency, e.settlement_currency]
        if any(c is None for c in currencies):
            report["issues"].append({
                "type": "missing_currency",
                "severity": "P2",
                "event": e.name,
                "action": "Populate all currency fields.",
            })
        # FX snapshot completeness + finite positive rate
        if e.fx_from_currency or e.fx_to_currency or e.fx_rate:
            if not (e.fx_from_currency and e.fx_to_currency and e.fx_rate):
                report["issues"].append({
                    "type": "incomplete_fx_snapshot",
                    "severity": "P2",
                    "event": e.name,
                    "action": "Backfill FX snapshot from source document if available.",
                })
            else:
                if e.fx_from_currency == e.fx_to_currency:
                    if flt(e.fx_rate) != 1.0:
                        report["issues"].append({
                            "type": "inconsistent_same_currency_rate",
                            "severity": "P2",
                            "event": e.name,
                            "action": "Same-currency FX rate must be 1.0.",
                        })
                if not _is_finite_positive(e.fx_rate):
                    report["issues"].append({
                        "type": "invalid_fx_rate",
                        "severity": "P2",
                        "event": e.name,
                        "action": "FX rate must be finite and positive.",
                    })

    # 8. GL idempotency records
    try:
        idem_rows = frappe.get_all(
            "Commission GL Idempotency",
            fields=["name", "idempotency_key", "journal_entry", "posting_fingerprint",
                    "posting_purpose", "source_event", "company", "posting_fingerprint"],
        )
        report["inspected"]["gl_idempotency"] = len(idem_rows)
        seen_keys = {}
        for r in idem_rows:
            if not r.posting_fingerprint:
                report["issues"].append({
                    "type": "missing_gl_fingerprint",
                    "severity": "P2",
                    "record": r.name,
                    "idempotency_key": r.idempotency_key,
                    "action": "Backfill fingerprint from linked Journal Entry or validate manually.",
                })
            if r.journal_entry:
                je = frappe.db.get_value(
                    "Journal Entry", r.journal_entry,
                    ["docstatus", "company", "user_remark"], as_dict=True
                )
                if not je:
                    report["issues"].append({
                        "type": "missing_linked_je",
                        "severity": "P1",
                        "record": r.name,
                        "journal_entry": r.journal_entry,
                        "action": "Linked Journal Entry does not exist.",
                    })
                else:
                    if je.docstatus != 1:
                        report["issues"].append({
                            "type": "unsubmitted_gl_entry",
                            "severity": "P1",
                            "record": r.name,
                            "journal_entry": r.journal_entry,
                            "docstatus": je.docstatus,
                            "action": "Investigate and resubmit or cancel the Journal Entry.",
                        })
                    if r.company and je.company and r.company != je.company:
                        report["issues"].append({
                            "type": "gl_company_mismatch",
                            "severity": "P1",
                            "record": r.name,
                            "action": "GL entry company must match idempotency record.",
                        })
            # Duplicate idempotency keys
            seen_keys.setdefault(r.idempotency_key, []).append(r.name)
        for key, names in seen_keys.items():
            if len(names) > 1:
                report["issues"].append({
                    "type": "duplicate_idempotency_key",
                    "severity": "P1",
                    "idempotency_key": key,
                    "records": names,
                    "action": "Remove duplicate idempotency records; verify Journal Entries.",
                })
    except Exception as e:
        report["issues"].append({
            "type": "gl_idempotency_audit_error",
            "severity": "P2",
            "error": str(e),
        })

    # 9. Settlement reconciliation (all derived fields)
    for sett in settlements:
        try:
            from sales_commission_management.sales_commission_management.payout.status import (
                compute_payout_status,
            )

            ps = compute_payout_status(sett.name, for_update=False)
            precision = _cp(sett.commission_currency) if sett.commission_currency else 2
            diffs = []
            if abs(flt(sett.total_paid or 0, precision) - flt(ps.get("actual_paid_amount", 0), precision)) > 0.005:
                diffs.append("total_paid: stored={0} derived={1}".format(
                    sett.total_paid, ps.get("actual_paid_amount")
                ))
            if abs(flt(sett.outstanding or 0, precision) - flt(ps.get("outstanding_payout_amount", 0), precision)) > 0.005:
                diffs.append("outstanding: stored={0} derived={1}".format(
                    sett.outstanding, ps.get("outstanding_payout_amount")
                ))
            if abs(flt(sett.actual_paid_amount or 0, precision) - flt(ps.get("actual_paid_amount", 0), precision)) > 0.005:
                diffs.append("actual_paid_amount: stored={0} derived={1}".format(
                    sett.actual_paid_amount, ps.get("actual_paid_amount")
                ))
            if abs(flt(sett.outstanding_payout_amount or 0, precision) - flt(ps.get("outstanding_payout_amount", 0), precision)) > 0.005:
                diffs.append("outstanding_payout_amount: stored={0} derived={1}".format(
                    sett.outstanding_payout_amount, ps.get("outstanding_payout_amount")
                ))
            if abs(flt(sett.recoverable_amount or 0, precision) - flt(ps.get("recoverable_amount", 0), precision)) > 0.005:
                diffs.append("recoverable_amount: stored={0} derived={1}".format(
                    sett.recoverable_amount, ps.get("recoverable_amount")
                ))
            if sett.payout_status and sett.payout_status != ps.get("payout_status"):
                diffs.append("payout_status: stored={0} derived={1}".format(
                    sett.payout_status, ps.get("payout_status")
                ))
            if diffs:
                report["issues"].append({
                    "type": "settlement_balance_mismatch",
                    "severity": "P1",
                    "settlement": sett.name,
                    "differences": diffs,
                    "action": "Refresh payout status fields via refresh_settlement_payout_status().",
                })
        except Exception as e:
            report["issues"].append({
                "type": "settlement_audit_error",
                "severity": "P2",
                "settlement": sett.name,
                "error": str(e),
            })

    # 10. Inconsistent currencies / settlement currency missing
    for sett in settlements:
        if not sett.commission_currency:
            report["issues"].append({
                "type": "missing_settlement_currency",
                "severity": "P2",
                "settlement": sett.name,
                "action": "Set commission currency on the settlement.",
            })

    # 11. Financial impact by company and currency (actual monetary differences)
    impact = {}
    # Initialize impact buckets for all companies/currencies present
    for sett in settlements:
        key = "{0}:{1}".format(sett.company, sett.commission_currency)
        impact.setdefault(key, {
            "company": sett.company,
            "currency": sett.commission_currency,
            "settlements_affected": 0,
            "total_payable_diff": 0.0,
            "actual_paid_diff": 0.0,
            "outstanding_diff": 0.0,
            "recovery_diff": 0.0,
            "settlements": [],
            "calculation_errors": [],
        })
    for issue in report["issues"]:
        if issue.get("type") == "settlement_balance_mismatch":
            sett = frappe.get_doc("Commission Settlement", issue["settlement"])
            key = "{0}:{1}".format(sett.company, sett.commission_currency)
            if key in impact:
                impact[key]["settlements_affected"] += 1
                impact[key]["settlements"].append(issue["settlement"])
    # Compute actual monetary diffs per company/currency (fail-closed: record errors)
    audit_incomplete = False
    for sett in settlements:
        try:
            from sales_commission_management.sales_commission_management.payout.status import (
                compute_payout_status,
            )

            ps = compute_payout_status(sett.name, for_update=False)
            precision = _cp(sett.commission_currency) if sett.commission_currency else 2
            key = "{0}:{1}".format(sett.company, sett.commission_currency)
            if key not in impact:
                continue
            # Signed variances (stored - derived)
            impact[key]["total_payable_diff"] = flt(
                impact[key]["total_payable_diff"]
                + (flt(sett.total_payable or 0, precision) - flt(ps.get("total_payable", 0), precision)),
                precision,
            )
            impact[key]["actual_paid_diff"] = flt(
                impact[key]["actual_paid_diff"]
                + (flt(sett.actual_paid_amount or 0, precision) - flt(ps.get("actual_paid_amount", 0), precision)),
                precision,
            )
            impact[key]["outstanding_diff"] = flt(
                impact[key]["outstanding_diff"]
                + (flt(sett.outstanding or 0, precision) - flt(ps.get("outstanding_payout_amount", 0), precision)),
                precision,
            )
            impact[key]["recovery_diff"] = flt(
                impact[key]["recovery_diff"]
                + (flt(sett.recoverable_amount or 0, precision) - flt(ps.get("recoverable_amount", 0), precision)),
                precision,
            )
        except Exception as e:
            audit_incomplete = True
            key = "{0}:{1}".format(sett.company, sett.commission_currency)
            if key in impact:
                impact[key]["calculation_errors"].append(
                    {"settlement": sett.name, "error": str(e)}
                )
            report["issues"].append({
                "type": "financial_impact_calculation_error",
                "severity": "P1",
                "settlement": sett.name,
                "error": str(e),
                "action": "Fix calculation error before relying on financial impact.",
            })
    if audit_incomplete:
        report["audit_incomplete"] = True
        report["issues"].append({
            "type": "audit_incomplete",
            "severity": "P1",
            "action": "One or more financial calculations failed; audit results are incomplete.",
        })
    report["financial_impact"] = impact

    # 12. Records requiring manual Finance review
    manual = []
    for issue in report["issues"]:
        if issue.get("severity") == "P1":
            manual.append({
                "type": issue["type"],
                "reference": issue.get("settlement") or issue.get("event") or issue.get("record"),
                "reason": issue.get("action", "Requires manual review."),
            })
    report["manual_review"] = manual

    # 13. Proposed actions
    report["proposed_actions"] = [
        "Run refresh_settlement_payout_status() on all submitted settlements to sync derived fields.",
        "Investigate and reconcile duplicate event keys before backfill.",
        "Backfill missing GL fingerprints from linked Journal Entries.",
        "Verify source document references for all payout events.",
        "Obtain Finance sign-off on reconciliation discrepancies before applying corrections.",
    ]

    # Summary counts
    report["summary"] = {
        "records_inspected": {
            "settlements": len(settlements),
            "payout_events": len(events),
            "gl_idempotency": report["inspected"].get("gl_idempotency", 0),
        },
        "issues_found": len(report["issues"]),
        "p1_issues": sum(1 for i in report["issues"] if i.get("severity") == "P1"),
        "p2_issues": sum(1 for i in report["issues"] if i.get("severity") == "P2"),
        "manual_review_count": len(manual),
    }

    return report


@frappe.whitelist()
def run_audit_api():
    """Whitelisted entry point for UI/CLI invocation (authorized roles only)."""
    _check_api_authorization()
    return run_audit()