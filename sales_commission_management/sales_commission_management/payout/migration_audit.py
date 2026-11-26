"""Read-only migration audit for the Commission Payout Event ledger (P3.6.1).

This module performs a non-destructive analysis of existing records and
produces a structured report.  It never modifies the database.

Usage (bench):
    bench --site <site> execute \\
        sales_commission_management.sales_commission_management.payout.migration_audit.run_audit
"""
import frappe
from frappe import _
from frappe.utils import flt


def _safe(fn, *a, **kw):
    try:
        return fn(*a, **kw), None
    except Exception as e:
        return None, str(e)


def run_audit():
    """Run the read-only migration audit and return a structured report dict."""
    report = {
        "inspected": {},
        "issues": [],
        "financial_impact": {},
        "proposed_actions": [],
        "manual_review": [],
    }

    # 1. Submitted settlements
    settlements = frappe.get_all(
        "Commission Settlement",
        filters={"docstatus": 1},
        fields=[
            "name", "company", "status", "total_payable", "total_paid",
            "outstanding", "payout_status", "actual_paid_amount",
            "outstanding_payout_amount", "recoverable_amount",
            "commission_currency", "beneficiary_type", "sales_person",
            "sales_partner", "employee", "supplier",
        ],
    )
    report["inspected"]["settlements"] = len(settlements)

    # 2. Payout events
    events = frappe.get_all(
        "Commission Payout Event",
        filters={"docstatus": 1},
        fields=[
            "name", "event_type", "settlement", "commission_entry",
            "company", "source_doctype", "source_name", "source_row",
            "reversal_of", "recovery_of", "event_key",
            "commission_currency", "commission_amount",
            "company_currency", "company_amount",
            "settlement_currency", "settlement_amount",
            "fx_from_currency", "fx_to_currency", "fx_rate",
            "evidence_level", "posting_date",
        ],
    )
    report["inspected"]["payout_events"] = len(events)

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

    # 4. Missing/invalid source references
    for e in events:
        if e.source_doctype and e.source_name:
            try:
                if not frappe.db.exists(e.source_doctype, e.source_name):
                    report["issues"].append({
                        "type": "missing_source_reference",
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

    # 5. Payout reversals without valid original
    event_names = {e.name for e in events}
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

    # 7. Missing FX snapshots
    for e in events:
        if e.fx_from_currency or e.fx_to_currency or e.fx_rate:
            if not (e.fx_from_currency and e.fx_to_currency and e.fx_rate):
                report["issues"].append({
                    "type": "incomplete_fx_snapshot",
                    "severity": "P2",
                    "event": e.name,
                    "action": "Backfill FX snapshot from source document if available.",
                })

    # 8. GL idempotency records with missing fingerprints
    try:
        idem_rows = frappe.get_all(
            "Commission GL Idempotency",
            fields=["name", "idempotency_key", "journal_entry", "posting_fingerprint",
                    "posting_purpose", "source_event", "company"],
        )
        report["inspected"]["gl_idempotency"] = len(idem_rows)
        for r in idem_rows:
            if not r.posting_fingerprint:
                report["issues"].append({
                    "type": "missing_gl_fingerprint",
                    "severity": "P2",
                    "record": r.name,
                    "idempotency_key": r.idempotency_key,
                    "action": "Backfill fingerprint from linked Journal Entry or validate manually.",
                })
            # Verify linked JE is submitted
            if r.journal_entry:
                je_status = frappe.db.get_value("Journal Entry", r.journal_entry, "docstatus")
                if je_status != 1:
                    report["issues"].append({
                        "type": "unsubmitted_gl_entry",
                        "severity": "P1",
                        "record": r.name,
                        "journal_entry": r.journal_entry,
                        "docstatus": je_status,
                        "action": "Investigate and resubmit or cancel the Journal Entry.",
                    })
    except Exception as e:
        report["issues"].append({
            "type": "gl_idempotency_audit_error",
            "severity": "P2",
            "error": str(e),
        })

    # 9. Differences between stored settlement fields and event-derived balances
    for sett in settlements:
        try:
            from sales_commission_management.sales_commission_management.payout.status import (
                compute_payout_status,
            )

            ps = compute_payout_status(sett.name)
            diffs = []
            if abs(flt(sett.total_paid or 0) - flt(ps.get("actual_paid_amount", 0))) > 0.005:
                diffs.append("total_paid: stored={0} derived={1}".format(
                    sett.total_paid, ps.get("actual_paid_amount")
                ))
            if abs(flt(sett.outstanding or 0) - flt(ps.get("outstanding_payout_amount", 0))) > 0.005:
                diffs.append("outstanding: stored={0} derived={1}".format(
                    sett.outstanding, ps.get("outstanding_payout_amount")
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

    # 10. Inconsistent currencies
    for sett in settlements:
        curr = sett.commission_currency
        if not curr:
            report["issues"].append({
                "type": "missing_settlement_currency",
                "severity": "P2",
                "settlement": sett.name,
                "action": "Set commission currency on the settlement.",
            })

    # 11. Financial impact summary by company and currency
    impact = {}
    for issue in report["issues"]:
        if issue.get("type") == "settlement_balance_mismatch":
            sett = frappe.get_doc("Commission Settlement", issue["settlement"])
            key = "{0}:{1}".format(sett.company, sett.commission_currency)
            impact.setdefault(key, {"count": 0, "settlements": []})
            impact[key]["count"] += 1
            impact[key]["settlements"].append(issue["settlement"])
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
    """Whitelisted entry point for UI/CLI invocation."""
    return run_audit()