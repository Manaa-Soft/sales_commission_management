"""Read-only migration audit for the Commission Payout Event ledger (P3.6.1).

This module performs a non-destructive analysis of existing records and
produces a structured report.  It never modifies the database.
"""
import frappe
from frappe import _
from frappe.utils import flt, cint
from sales_commission_management.sales_commission_management.utils import (
    currency_precision as _cp,
)


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
    roles = set(frappe.get_roles(frappe.session.user))
    if "System Manager" in roles:
        return None
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
    report = {
        "inspected": {},
        "issues": [],
        "financial_impact": {},
        "proposed_actions": [],
        "manual_review": [],
    }
    audit_incomplete = False

    if companies is None:
        companies = _authorized_companies() if frappe.session.user != "Guest" else None
    if companies == []:
        report["audit_incomplete"] = False
        report["summary"] = {"records_inspected": {"settlements": 0, "payout_events": 0, "gl_idempotency": 0},
                            "issues_found": 0, "p1_issues": 0, "p2_issues": 0, "manual_review_count": 0}
        return report

    sett_filters = {"docstatus": 1}
    if companies:
        sett_filters["company"] = ["in", companies]

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
    authorized_settlement_names = {s.name for s in settlements}

    # Payout events: include events whose company is authorized OR whose
    # settlement is an authorized settlement (detects wrong company values).
    if companies is None:
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
                "evidence_level", "posting_date", "beneficiary",
            ],
        )
    else:
        events = frappe.get_all(
            "Commission Payout Event",
            filters={"docstatus": 1},
            or_filters=[
                {"company": ["in", companies]},
                {"settlement": ["in", list(authorized_settlement_names)]},
            ],
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

    # Duplicate event keys
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

    # Source reference validation (all events)
    for e in events:
        # Missing required source fields
        if not e.source_doctype or not e.source_name:
            report["issues"].append({
                "type": "missing_source_reference",
                "severity": "P1",
                "event": e.name,
                "action": "Populate source DocType and source name.",
            })
        else:
            try:
                if not frappe.db.exists(e.source_doctype, e.source_name):
                    report["issues"].append({
                        "type": "missing_source_document",
                        "severity": "P1",
                        "event": e.name,
                        "source": "{0}:{1}".format(e.source_doctype, e.source_name),
                        "action": "Verify source document exists.",
                    })
            except Exception as ex:
                audit_incomplete = True
                report["issues"].append({
                    "type": "source_reference_check_error",
                    "severity": "P2",
                    "event": e.name,
                    "error": str(ex),
                    "action": "Source reference check failed.",
                })

        # Payment Entry source-row ownership
        if e.source_doctype == "Payment Entry":
            if not e.source_row:
                report["issues"].append({
                    "type": "missing_source_row",
                    "severity": "P2",
                    "event": e.name,
                    "action": "Populate the exact Payment Entry Reference row.",
                })
            else:
                try:
                    row_parent = frappe.db.get_value("Payment Entry Reference", e.source_row, "parent")
                    row_ref_dt = frappe.db.get_value("Payment Entry Reference", e.source_row, "reference_doctype")
                    row_ref_name = frappe.db.get_value("Payment Entry Reference", e.source_row, "reference_name")
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
                    # Explicitly detect missing reference fields.
                    if not row_ref_dt:
                        report["issues"].append({
                            "type": "missing_source_row_doctype",
                            "severity": "P1",
                            "event": e.name,
                            "source_row": e.source_row,
                            "action": "Payment Entry Reference row is missing reference_doctype.",
                        })
                    if not row_ref_name:
                        report["issues"].append({
                            "type": "missing_source_row_reference",
                            "severity": "P1",
                            "event": e.name,
                            "source_row": e.source_row,
                            "action": "Payment Entry Reference row is missing reference_name.",
                        })
                    # Validate against settlement Dynamic Link.
                    if e.settlement:
                        sett_info = frappe.db.get_value(
                            "Commission Settlement", e.settlement,
                            ["settlement_document", "settlement_document_type", "company"],
                            as_dict=True,
                        )
                        if not sett_info:
                            report["issues"].append({
                                "type": "missing_linked_settlement",
                                "severity": "P1",
                                "event": e.name,
                                "settlement": e.settlement,
                                "action": "Linked Commission Settlement does not exist.",
                            })
                        else:
                            if sett_info.settlement_document_type and row_ref_dt and row_ref_dt != sett_info.settlement_document_type:
                                report["issues"].append({
                                    "type": "source_row_doctype_mismatch",
                                    "severity": "P1",
                                    "event": e.name,
                                    "action": "Source row reference DocType must match settlement document type.",
                                })
                            if sett_info.settlement_document and row_ref_name and row_ref_name != sett_info.settlement_document:
                                report["issues"].append({
                                    "type": "source_row_reference_mismatch",
                                    "severity": "P1",
                                    "event": e.name,
                                    "action": "Source row must reference the settlement's Purchase Invoice.",
                                })
                            if sett_info.company and e.company and sett_info.company != e.company:
                                report["issues"].append({
                                    "type": "event_settlement_company_mismatch",
                                    "severity": "P1",
                                    "event": e.name,
                                    "action": "Payout event company must match settlement company.",
                                })
                            # Source PE company consistency.
                            try:
                                pe_company = frappe.db.get_value("Payment Entry", e.source_name, "company")
                                if pe_company and e.company and pe_company != e.company:
                                    report["issues"].append({
                                        "type": "source_pe_company_mismatch",
                                        "severity": "P1",
                                        "event": e.name,
                                        "action": "Source Payment Entry company must match payout event company.",
                                    })
                            except Exception as ex:
                                audit_incomplete = True
                                report["issues"].append({
                                    "type": "source_pe_check_error",
                                    "severity": "P2",
                                    "event": e.name,
                                    "error": str(ex),
                                    "action": "Payment Entry company lookup failed.",
                                })
                except Exception as ex:
                    audit_incomplete = True
                    report["issues"].append({
                        "type": "source_row_check_error",
                        "severity": "P2",
                        "event": e.name,
                        "error": str(ex),
                        "action": "Source row validation failed.",
                    })

        # General event-to-settlement validation (ALL event types)
        if not e.settlement:
            report["issues"].append({
                "type": "missing_settlement_reference",
                "severity": "P1",
                "event": e.name,
                "action": "Payout event must reference a Commission Settlement.",
            })
        else:
            try:
                sett_company = frappe.db.get_value("Commission Settlement", e.settlement, "company")
                if sett_company is None:
                    report["issues"].append({
                        "type": "missing_linked_settlement",
                        "severity": "P1",
                        "event": e.name,
                        "settlement": e.settlement,
                        "action": "Linked Commission Settlement does not exist.",
                    })
                elif sett_company and e.company and sett_company != e.company:
                    report["issues"].append({
                        "type": "event_settlement_company_mismatch",
                        "severity": "P1",
                        "event": e.name,
                        "action": "Payout event company must match settlement company.",
                    })
            except Exception as ex:
                audit_incomplete = True
                report["issues"].append({
                    "type": "settlement_lookup_error",
                    "severity": "P2",
                    "event": e.name,
                    "error": str(ex),
                    "action": "Settlement lookup failed.",
                })

    # Payout reversals validation
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

    # Recovery events validation
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
            if orig:
                if orig.event_type != "Payout":
                    report["issues"].append({
                        "type": "invalid_recovery_reference",
                        "severity": "P1",
                        "event": e.name,
                        "recovery_of": e.recovery_of,
                        "action": "Recovery must reference a Payout event.",
                    })
                if e.company != orig.company:
                    report["issues"].append({
                        "type": "recovery_company_mismatch",
                        "severity": "P1",
                        "event": e.name,
                        "action": "Recovery company must match original payout.",
                    })
                if e.commission_entry != orig.commission_entry:
                    report["issues"].append({
                        "type": "recovery_entry_mismatch",
                        "severity": "P1",
                        "event": e.name,
                        "action": "Recovery commission entry must match original payout.",
                    })
                if e.settlement != orig.settlement:
                    report["issues"].append({
                        "type": "recovery_settlement_mismatch",
                        "severity": "P1",
                        "event": e.name,
                        "action": "Recovery settlement must match original payout.",
                    })

    # Duplicate event keys
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

    # FX and currency validation
    for e in events:
        for curr_field in ("commission_currency", "company_currency", "settlement_currency"):
            val = getattr(e, curr_field, None)
            if val is None or (isinstance(val, str) and val.strip() == ""):
                report["issues"].append({
                    "type": "missing_currency",
                    "severity": "P2",
                    "event": e.name,
                    "action": "Populate {0}.".format(curr_field),
                })
        comm_curr = e.commission_currency
        comp_curr = e.company_currency
        needs_fx = comm_curr and comp_curr and comm_curr != comp_curr
        has_fx = bool(e.fx_from_currency and e.fx_to_currency and e.fx_rate)
        if needs_fx and not has_fx:
            report["issues"].append({
                "type": "missing_fx_snapshot",
                "severity": "P2",
                "event": e.name,
                "action": "FX snapshot required when commission and company currencies differ.",
            })
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
                # FX direction: validate from/to matches the actual conversion
                # convention (commission -> company), not either direction.
                if e.fx_from_currency and e.fx_to_currency:
                    valid_pairs = {
                        (e.commission_currency, e.company_currency),
                        (e.settlement_currency, e.company_currency),
                    }
                    if (e.fx_from_currency, e.fx_to_currency) not in valid_pairs:
                        report["issues"].append({
                            "type": "fx_direction_mismatch",
                            "severity": "P2",
                            "event": e.name,
                            "action": "FX from/to currencies must match the commission->company (or settlement->company) conversion convention.",
                        })

    # 8. GL idempotency records (company-scoped)
    try:
        # Database-level company filtering: authorized companies OR records linked
        # to authorized payout events.
        authorized_event_names = {e.name for e in events}
        if companies is None:
            idem_rows = frappe.get_all(
                "Commission GL Idempotency",
                fields=["name", "idempotency_key", "journal_entry", "posting_fingerprint",
                        "posting_purpose", "source_event", "company"],
            )
        else:
            # Use or_filters for database-level scoping.
            idem_rows = frappe.get_all(
                "Commission GL Idempotency",
                filters={"company": ["in", companies]},
                or_filters={"source_event": ["in", list(authorized_event_names)]} if authorized_event_names else None,
                fields=["name", "idempotency_key", "journal_entry", "posting_fingerprint",
                        "posting_purpose", "source_event", "company"],
            )
            # Also include records with missing company that link to authorized events.
            if authorized_event_names:
                extra = frappe.get_all(
                    "Commission GL Idempotency",
                    filters={"company": ["in", ["", None]]},
                    or_filters={"source_event": ["in", list(authorized_event_names)]},
                    fields=["name", "idempotency_key", "journal_entry", "posting_fingerprint",
                            "posting_purpose", "source_event", "company"],
                )
                existing_names = {r.name for r in idem_rows}
                for r in extra:
                    if r.name not in existing_names:
                        idem_rows.append(r)
        report["inspected"]["gl_idempotency"] = len(idem_rows)
        seen_keys = {}
        for r in idem_rows:
            if not r.journal_entry:
                report["issues"].append({
                    "type": "missing_linked_je",
                    "severity": "P1",
                    "record": r.name,
                    "idempotency_key": r.idempotency_key,
                    "action": "Idempotency record has no linked Journal Entry.",
                })
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
        audit_incomplete = True
        report["issues"].append({
            "type": "gl_idempotency_audit_error",
            "severity": "P1",
            "error": str(e),
            "action": "GL audit failed; results are incomplete.",
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
            if abs(flt(sett.total_paid or 0, precision) - flt(ps.get("actual_paid_amount", 0), precision)) > 0:
                diffs.append("total_paid: stored={0} derived={1}".format(
                    sett.total_paid, ps.get("actual_paid_amount")
                ))
            if abs(flt(sett.outstanding or 0, precision) - flt(ps.get("outstanding_payout_amount", 0), precision)) > 0:
                diffs.append("outstanding: stored={0} derived={1}".format(
                    sett.outstanding, ps.get("outstanding_payout_amount")
                ))
            if abs(flt(sett.actual_paid_amount or 0, precision) - flt(ps.get("actual_paid_amount", 0), precision)) > 0:
                diffs.append("actual_paid_amount: stored={0} derived={1}".format(
                    sett.actual_paid_amount, ps.get("actual_paid_amount")
                ))
            if abs(flt(sett.outstanding_payout_amount or 0, precision) - flt(ps.get("outstanding_payout_amount", 0), precision)) > 0:
                diffs.append("outstanding_payout_amount: stored={0} derived={1}".format(
                    sett.outstanding_payout_amount, ps.get("outstanding_payout_amount")
                ))
            if abs(flt(sett.recoverable_amount or 0, precision) - flt(ps.get("recoverable_amount", 0), precision)) > 0:
                diffs.append("recoverable_amount: stored={0} derived={1}".format(
                    sett.recoverable_amount, ps.get("recoverable_amount")
                ))
            derived_status = ps.get("payout_status") or "Not Paid"
            if not sett.payout_status or sett.payout_status != derived_status:
                diffs.append("payout_status: stored={0} derived={1}".format(
                    sett.payout_status, derived_status
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
            audit_incomplete = True
            report["issues"].append({
                "type": "settlement_audit_error",
                "severity": "P1",
                "settlement": sett.name,
                "error": str(e),
                "action": "Settlement audit calculation failed; results are incomplete.",
            })

    # 10. Inconsistent currencies / settlement currency missing
    for sett in settlements:
        if not sett.commission_currency or (isinstance(sett.commission_currency, str) and sett.commission_currency.strip() == ""):
            report["issues"].append({
                "type": "missing_settlement_currency",
                "severity": "P2",
                "settlement": sett.name,
                "action": "Set commission currency on the settlement.",
            })

    # 11. Financial impact by company and currency (actual monetary differences)
    impact = {}
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
            "gross_absolute_diff": 0.0,
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
            payable_diff = flt(sett.total_payable or 0, precision) - flt(ps.get("total_payable", 0), precision)
            paid_diff = flt(sett.actual_paid_amount or 0, precision) - flt(ps.get("actual_paid_amount", 0), precision)
            outstanding_diff = flt(sett.outstanding or 0, precision) - flt(ps.get("outstanding_payout_amount", 0), precision)
            recovery_diff = flt(sett.recoverable_amount or 0, precision) - flt(ps.get("recoverable_amount", 0), precision)
            impact[key]["total_payable_diff"] = flt(impact[key]["total_payable_diff"] + payable_diff, precision)
            impact[key]["actual_paid_diff"] = flt(impact[key]["actual_paid_diff"] + paid_diff, precision)
            impact[key]["outstanding_diff"] = flt(impact[key]["outstanding_diff"] + outstanding_diff, precision)
            impact[key]["recovery_diff"] = flt(impact[key]["recovery_diff"] + recovery_diff, precision)
            impact[key]["gross_absolute_diff"] = flt(
                impact[key]["gross_absolute_diff"] + abs(payable_diff) + abs(paid_diff) + abs(outstanding_diff) + abs(recovery_diff),
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

    report["audit_incomplete"] = audit_incomplete

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