frappe.ui.form.on("Commission Settlement", {
    refresh: function (frm) {
        if (frm.doc.docstatus === 0) {
            frm.add_custom_button(__("Fetch Eligible Entries"), function () {
                fetch_eligible_entries(frm);
            });
        }
        _recompute_settlement_totals(frm);
    },
    company: function (frm) {
        _maybe_auto_fetch(frm);
    },
    beneficiary_type: function (frm) {
        _maybe_auto_fetch(frm);
    },
    sales_person: function (frm) {
        _maybe_auto_fetch(frm);
    },
    sales_partner: function (frm) {
        _maybe_auto_fetch(frm);
    },
    period_from: function (frm) {
        _maybe_auto_fetch(frm);
    },
    period_to: function (frm) {
        _maybe_auto_fetch(frm);
    },
    commission_currency: function (frm) {
        _maybe_auto_fetch(frm);
    },
    entries: function (frm) {
        _recompute_settlement_totals(frm);
    },
});

function _required_for_fetch(frm) {
    if (!frm.doc.company) return false;
    if (!frm.doc.beneficiary_type) return false;
    if (frm.doc.beneficiary_type === "Sales Person" && !frm.doc.sales_person) return false;
    if (frm.doc.beneficiary_type === "Sales Partner" && !frm.doc.sales_partner) return false;
    return true;
}

function _maybe_auto_fetch(frm) {
    // Only auto-fetch when all key filters are present and the doc is a draft
    // with an empty entries table, to avoid clobbering user edits.
    if (frm.doc.docstatus !== 0) return;
    if (!frm.doc.entries || frm.doc.entries.length === 0) {
        if (_required_for_fetch(frm)) {
            fetch_eligible_entries(frm);
        }
    }
}

function fetch_eligible_entries(frm) {
    if (!_required_for_fetch(frm)) {
        frappe.msgprint(__("Please set Company, Beneficiary Type and Beneficiary first."));
        return;
    }
    frappe.call({
        method: "sales_commission_management.sales_commission_management.doctype.commission_settlement.commission_settlement.get_eligible_commission_entries",
        args: {
            company: frm.doc.company,
            beneficiary_type: frm.doc.beneficiary_type,
            sales_person: frm.doc.sales_person,
            sales_partner: frm.doc.sales_partner,
            period_from: frm.doc.period_from,
            period_to: frm.doc.period_to,
            commission_currency: frm.doc.commission_currency,
            exclude_settlement: frm.doc.name,
        },
        freeze: true,
        freeze_message: __("Fetching eligible commission entries..."),
        callback: function (r) {
            if (!r.message) {
                frm.clear_table("entries");
                frm.refresh_field("entries");
                _recompute_settlement_totals(frm);
                return;
            }
            frm.clear_table("entries");
            r.message.forEach(function (row) {
                let d = frm.add_child("entries");
                d.commission_ledger_entry = row.commission_ledger_entry;
                d.original_source_invoice = row.original_source_invoice;
                d.beneficiary = row.beneficiary;
                d.earned_balance = row.earned_balance;
                d.eligible_balance = row.eligible_balance;
                d.reserved_amount = row.reserved_amount;
                d.already_settled_amount = row.already_settled_amount;
                d.amount_included = row.amount_included;
            });
            frm.refresh_field("entries");
            _recompute_settlement_totals(frm);
            frappe.show_alert({
                message: __("Fetched {0} eligible commission entries.", [r.message.length]),
                indicator: "green",
            });
        },
    });
}

function _recompute_settlement_totals(frm) {
    let total_earned = 0;
    let total_payable = 0;
    let total_reserved = 0;
    let total_paid = 0;
    (frm.doc.entries || []).forEach(function (row) {
        total_earned += flt(row.earned_balance);
        total_payable += flt(row.amount_included) || flt(row.eligible_balance);
        total_reserved += flt(row.reserved_amount);
        total_paid += flt(row.already_settled_amount);
    });
    frm.set_value("total_earned", flt(total_earned, 2));
    frm.set_value("total_payable", flt(total_payable, 2));
    frm.set_value("total_reserved", flt(total_reserved, 2));
    frm.set_value("total_paid", flt(total_paid, 2));
    frm.set_value("outstanding", flt(total_payable - total_paid, 2));
}
