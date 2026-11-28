app_name = "sales_commission_management"
app_title = "Sales Commission Management for ERPNext"
app_publisher = "Manaa-Soft"
app_description = "Advanced multi-tier commission engine for ERPNext v15/v16"
app_icon = "fa fa-percent"
app_color = "#2980b9"
app_email = "manaamnaa2018@gmail.com"
app_license = "mit"

add_to_apps_screen = [
    {
        "name": app_name,
        "logo": "/assets/sales_commission_management/images/icon.svg",
        "title": app_title,
        "route": "/app/sales-commission-management",
        "sequence_id": 100,
    }
]

doc_events = {
    "Sales Invoice": {
        "before_submit": "sales_commission_management.sales_commission_management.doctype.commission_entry.commission_entry.neutralize_native_commission",
        "on_submit": "sales_commission_management.sales_commission_management.doctype.commission_entry.commission_entry.generate_commission_entries",
        "on_cancel": [
            "sales_commission_management.sales_commission_management.doctype.commission_entry.commission_entry.reverse_commission_entries",
            "sales_commission_management.sales_commission_management.doctype.commission_entry.commission_entry.reconcile_return_cancellation",
        ],
        "before_update_after_submit": "sales_commission_management.sales_commission_management.doctype.commission_entry.commission_entry.neutralize_native_commission_on_update",
    },
    "POS Invoice": {
        "on_submit": "sales_commission_management.sales_commission_management.doctype.commission_entry.commission_entry.generate_commission_entries",
        "on_cancel": "sales_commission_management.sales_commission_management.doctype.commission_entry.commission_entry.reverse_commission_entries",
    },
    "Payment Entry": {
        "before_submit": "sales_commission_management.sales_commission_management.doctype.commission_entry.commission_entry.lock_referenced_invoices",
        "on_submit": [
            "sales_commission_management.sales_commission_management.doctype.commission_entry.commission_entry.generate_commission_on_payment",
            "sales_commission_management.sales_commission_management.doctype.commission_entry.commission_entry.update_commission_payment_status",
            "sales_commission_management.sales_commission_management.payout.manual_payment.process_payment_entry_for_commission",
        ],
        "on_cancel": [
            "sales_commission_management.sales_commission_management.doctype.commission_entry.commission_entry.update_commission_payment_status",
        ],
        "before_cancel": [
            "sales_commission_management.sales_commission_management.doctype.commission_entry.commission_entry.lock_referenced_invoices",
            "sales_commission_management.sales_commission_management.payout.manual_payment.reverse_payment_entry_payouts",
        ],
    },
    "Journal Entry": {
        "before_cancel": [
            "sales_commission_management.sales_commission_management.payout.manual_payment.reverse_journal_entry_payouts",
        ],
        "on_submit": [
            "sales_commission_management.sales_commission_management.doctype.commission_entry.commission_entry.generate_commission_on_payment",
            "sales_commission_management.sales_commission_management.doctype.commission_entry.commission_entry.update_commission_payment_status",
            "sales_commission_management.sales_commission_management.payout.supplier_payment.process_supplier_payment_entry",
        ],
        "on_cancel": "sales_commission_management.sales_commission_management.doctype.commission_entry.commission_entry.update_commission_payment_status",
    },
}

scheduler_events = {
    "daily": [
        "sales_commission_management.sales_commission_management.doctype.commission_entry.commission_entry.recompute_period_slabs"
    ]
}

after_migrate = [
    "sales_commission_management.patches.migrate_commission_settings_legacy_data.ensure",
    "sales_commission_management.patches.ensure_commission_settlement_link_fields.execute",
    "sales_commission_management.patches.ensure_commission_attribution_snapshot_field.execute",
]

after_install = [
    "sales_commission_management.patches.migrate_commission_settings_legacy_data.ensure",
    "sales_commission_management.patches.ensure_commission_settlement_link_fields.execute",
    "sales_commission_management.patches.ensure_commission_attribution_snapshot_field.execute",
]
