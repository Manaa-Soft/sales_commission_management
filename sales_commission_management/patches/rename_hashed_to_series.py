# -*- coding: utf-8 -*-
import re
import frappe
from frappe.model.naming import make_autoname


HASH_RE = re.compile(r"^[0-9a-f]{10}$")


def _is_hash(name):
    return bool(name and HASH_RE.match(str(name)))


def _rename_hashed(doctype, series_pattern):
    """Rename hash-named records of `doctype` to series names. Idempotent."""
    names = frappe.get_all(doctype, filters={}, pluck="name") or []
    renamed = 0
    for old in names:
        if not _is_hash(old):
            continue
        try:
            new = make_autoname(series_pattern)
            frappe.rename_doc(doctype, old, new, force=True)
            renamed += 1
        except Exception:
            frappe.log_error(
                frappe.get_traceback(),
                "rename_hashed failed for {0} {1}".format(doctype, old),
            )
    if renamed:
        frappe.db.commit()
    return renamed


def execute():
    # Only rename when autoname is series-based (post-migration state).
    mapping = {
        "Commission Entry": "CE-.YYYY.-.####",
        "Commission Settlement": "CS-.YYYY.-.####",
        "Commission Accrual Batch": "CAB-.YYYY.-.####",
        "Commission Period": "CP-.YYYY.-.####",
        "Commission Target": "CT-.YYYY.-.####",
    }
    total = 0
    for doctype, series in mapping.items():
        if not frappe.db.exists("DocType", doctype):
            continue
        n = _rename_hashed(doctype, series)
        total += n
        if n:
            print("Renamed {0} {1} record(s) for {2}".format(n, doctype, series))
    # Commission Plan uses field:plan_name (user-entered); nothing to rename.
    print("rename_hashed_to_series: total renamed =", total)