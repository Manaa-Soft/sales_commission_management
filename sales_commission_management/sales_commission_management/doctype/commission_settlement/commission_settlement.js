frappe.ui.form.on("Commission Settlement", {
    refresh: function (frm) {
        if (frm.doc.docstatus === 0) {
            frm.add_custom_button(__("Fetch Eligible Entries"), function () {
                fetch_eligible_entries(frm);
            });
        }

        // View group: only documents that actually exist and are related.
        frm.add_custom_button(__("Commission Payout Events"), function () {
            frappe.set_route("List", "Commission Payout Event", {
                settlement: frm.doc.name,
            });
        }, __("View"));

        if (frm.doc.settlement_document_type && frm.doc.settlement_document) {
            frm.add_custom_button(__(frm.doc.settlement_document_type), function () {
                frappe.set_route(
                    "Form",
                    frm.doc.settlement_document_type,
                    frm.doc.settlement_document
                );
            }, __("View"));
        }

        if (frm.doc.settlement_journal_entry) {
            frm.add_custom_button(__("Settlement Journal Entry"), function () {
                frappe.set_route("Form", "Journal Entry", frm.doc.settlement_journal_entry);
            }, __("View"));
        }

        // Create group.
        var can_record_manual_payout =
            frm.doc.docstatus === 1 &&
            frm.doc.settlement_method === "Manual Approved Route" &&
            flt(frm.doc.outstanding_payout_amount) > 0 &&
            (frappe.session.user === "Administrator" ||
                frappe.user.has_role([
                    "System Manager",
                    "Accounts Manager",
                    "Commission Manager",
                ]));
        if (can_record_manual_payout) {
            frm.add_custom_button(__("Record Manual Payout"), function () {
                record_manual_payout_dialog(frm);
            }, __("Create"));

            frm.page.set_inner_btn_group_as_primary(__("Create"));
        }

        // Actions group.
        if (frm.doc.docstatus === 1) {
            frm.add_custom_button(__("Refresh Payout Status"), function () {
                refresh_payout_status(frm);
            }, __("Actions"));
        }

        _set_payout_indicator(frm);
        _filter_settlement_methods(frm);
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
    if (frm.doc.entries && frm.doc.entries.length > 0) {
        frappe.confirm(
            __("This will replace the existing entries. Continue?"),
            function () {
                _do_fetch_eligible_entries(frm);
            }
        );
        return;
    }
    _do_fetch_eligible_entries(frm);
}

function _do_fetch_eligible_entries(frm) {
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

function _filter_settlement_methods(frm) {
    var df = frm.get_field("settlement_method");
    if (!df) return;
    var base = ["Additional Salary", "Supplier Purchase Invoice", "Manual Approved Route"];
    df.df.options = base.join("\n");
    frm.refresh_field("settlement_method");
}

function _recompute_settlement_totals(frm) {
    // Submitted settlement totals are controlled by the server.
    // The payout-event ledger is authoritative for paid amounts.
    if (frm.doc.docstatus !== 0) {
        return;
    }

    let total_earned = 0;
    let total_payable = 0;
    let total_reserved = 0;
    let total_paid = 0;
    (frm.doc.entries || []).forEach(function (row) {
        total_earned += flt(row.earned_balance);
        var raw = row.amount_included;
        if (raw === null || raw === undefined || raw === "") {
            total_payable += flt(row.eligible_balance);
        } else {
            total_payable += flt(raw);
        }
        total_reserved += flt(row.reserved_amount);
        total_paid += flt(row.already_settled_amount);
    });
    frm.set_value("total_earned", flt(total_earned, 2));
    frm.set_value("total_payable", flt(total_payable, 2));
    frm.set_value("total_reserved", flt(total_reserved, 2));
    frm.set_value("total_paid", flt(total_paid, 2));
    frm.set_value("outstanding", flt(total_payable - total_paid, 2));
}

function record_manual_payout_dialog(frm) {
    var d = new frappe.ui.Dialog({
        title: __("Record Manual Payout"),
        fields: [
            { fieldname: "payment_date", fieldtype: "Date", label: __("Payment Date"), reqd: 1, default: frappe.datetime.get_today() },
            { fieldname: "amount", fieldtype: "Currency", label: __("Amount Paid"), reqd: 1, default: frm.doc.outstanding_payout_amount || frm.doc.outstanding },
            { fieldname: "source_doctype", fieldtype: "Link", label: __("Evidence DocType"), options: "DocType", reqd: 1 },
            { fieldname: "source_name", fieldtype: "Dynamic Link", label: __("Evidence Document"), options: "source_doctype", reqd: 1 },
            { fieldname: "remarks", fieldtype: "Small Text", label: __("Remarks") },
        ],
        primary_action_label: __("Record"),
        primary_action: function () {
            var v = d.get_values();
            if (!v) return;
            frappe.call({
                method: "sales_commission_management.sales_commission_management.doctype.commission_settlement.commission_settlement.record_manual_payout",
                args: {
                    settlement: frm.doc.name,
                    payment_date: v.payment_date,
                    amount: v.amount,
                    source_doctype: v.source_doctype,
                    source_name: v.source_name,
                    remarks: v.remarks,
                },
                freeze: true,
                freeze_message: __("Recording manual payout..."),
                callback: function () {
                    d.hide();
                    frm.reload_doc();
                    frappe.show_alert({ message: __("Manual payout recorded."), indicator: "green" });
                },
            });
        },
    });
    d.show();
}

function _set_payout_indicator(frm) {
    var status = frm.doc.payout_status || "Not Paid";
    var color_map = {
        "Not Paid": "red",
        "Payroll Accrued": "orange",
        "Disbursement Recorded": "blue",
        "Partially Paid": "orange",
        "Paid": "green",
        "Recovery Due": "red",
        "Recovered": "green",
        "Cancelled": "gray",
    };
    var color = color_map[status] || "gray";
    if (frm.doc.docstatus === 1) {
        // Preserve the native document lifecycle status alongside the payout
        // status so the submitted state is not hidden by the payout state.
        frm.page.set_indicator(__("Submitted") + " · " + __(status), color);
    }
}

function refresh_payout_status(frm) {
    frappe.call({
        method: "sales_commission_management.sales_commission_management.doctype.commission_settlement.commission_settlement.refresh_settlement_payout_status",
        args: { settlement: frm.doc.name },
        freeze: true,
        freeze_message: __("Refreshing payout status..."),
        callback: function () {
            frm.reload_doc();
            frappe.show_alert({
                message: __("Payout status refreshed."),
                indicator: "green",
            });
        },
    });
}
