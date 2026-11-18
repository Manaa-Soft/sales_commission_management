frappe.ui.form.on("Commission Accrual Batch", {
    refresh: function (frm) {
        if (frm.doc.docstatus === 0) {
            frm.add_custom_button(__("Fetch Unaccrued Entries"), function () {
                if (!frm.doc.company || !frm.doc.period_from || !frm.doc.period_to) {
                    frappe.msgprint(__("Company and period are required."));
                    return;
                }
                frappe.call({
                    method: "sales_commission_management.sales_commission_management.doctype.commission_accrual_batch.commission_accrual_batch.fetch_unaccrued_entries",
                    args: {
                        company: frm.doc.company,
                        period_from: frm.doc.period_from,
                        period_to: frm.doc.period_to,
                        exclude_batch: frm.doc.name,
                    },
                    callback: function (r) {
                        if (!r.message || !r.message.length) {
                            frappe.msgprint(__("No unaccrued commission entries found."));
                            return;
                        }
                        frm.clear_table("items");
                        (r.message || []).forEach(function (row) {
                            let child = frm.add_child("items");
                            child.commission_ledger_entry = row.commission_ledger_entry;
                            child.commission_currency = row.commission_currency;
                            child.accrual_amount = row.accrual_amount;
                            child.conversion_rate = row.conversion_rate;
                        });
                        frm.refresh_field("items");
                        frm.dirty();
                    },
                });
            });
        }
    },
    company: function (frm) {
        if (frm.doc.company) {
            frappe.db.get_value("Company", frm.doc.company, "default_currency", function (r) {
                if (r && r.default_currency) {
                    frm.set_value("accounting_currency", r.default_currency);
                }
            });
        }
    },
});
