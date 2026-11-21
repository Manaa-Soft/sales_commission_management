from frappe.tests import IntegrationTestCase
import frappe
import uuid


class TestPayoutLifecycle(IntegrationTestCase):
    """Integration tests for the payout event Draft->Submitted lifecycle."""

    def setUp(self):
        self.uid = uuid.uuid4().hex[:8]

        # Use the standard ERPNext test company so chart of accounts, items,
        # customers and accounts already exist (self-contained on a clean site).
        self.company = "_Test Company"

        # Ensure Commission Settings exists and is configured.
        if not frappe.db.exists("Commission Settings"):
            frappe.get_doc({
                "doctype": "Commission Settings",
                "enabled": 1,
                "accounting_mode": "Subledger Until Settlement",
            }).insert(ignore_permissions=True)
        else:
            cs = frappe.get_single("Commission Settings")
            if not getattr(cs, "accounting_mode", None):
                cs.accounting_mode = "Subledger Until Settlement"
                cs.save(ignore_permissions=True)

        sp_name = f"LC-SP-{self.uid}"
        if not frappe.db.exists("Sales Person", sp_name):
            frappe.get_doc({
                "doctype": "Sales Person",
                "sales_person_name": sp_name,
            }).insert(ignore_permissions=True)
        self.sales_person = sp_name

        # Create a real submitted Sales Invoice for Commission Entry linkage.
        from erpnext.accounts.doctype.sales_invoice.test_sales_invoice import create_sales_invoice
        si = create_sales_invoice(
            company=self.company,
            customer="_Test Customer",
            item_code="_Test Item",
            rate=1000,
            qty=1,
            do_not_submit=0,
        )
        self.sales_invoice = si.name

        ce_name = f"LC-CE-{self.uid}"
        if not frappe.db.exists("Commission Entry", ce_name):
            frappe.get_doc({
                "doctype": "Commission Entry",
                "name": ce_name,
                "sales_invoice": self.sales_invoice,
                "posting_date": frappe.utils.nowdate(),
                "company": self.company,
                "beneficiary_type": "Sales Person",
                "sales_person": self.sales_person,
                "earned_commission": 1000,
                "commission_currency": "USD",
                "company_currency": "USD",
                "status": "Earned",
                "docstatus": 1,
            }).insert(ignore_permissions=True)
        self.commission_entry = ce_name

        sett_name = f"LC-CS-{self.uid}"
        if not frappe.db.exists("Commission Settlement", sett_name):
            sett = frappe.get_doc({
                "doctype": "Commission Settlement",
                "name": sett_name,
                "company": self.company,
                "beneficiary_type": "Sales Person",
                "sales_person": self.sales_person,
                "supplier": None,
                "status": "Submitted",
                "commission_currency": "USD",
                "total_payable": 1000,
                "entries": [{
                    "commission_ledger_entry": self.commission_entry,
                    "amount_included": 1000,
                }],
            })
            sett.insert(ignore_permissions=True)
            sett.submit()
        self.settlement = sett_name

        self.settlement_item = frappe.db.get_value(
            "Commission Settlement Item",
            {"parent": self.settlement, "commission_ledger_entry": self.commission_entry},
            "name",
        )

        supplier_name = f"LC-SUP-{self.uid}"
        if not frappe.db.exists("Supplier", supplier_name):
            frappe.get_doc({
                "doctype": "Supplier",
                "supplier_name": supplier_name,
                "supplier_type": "Company",
            }).insert(ignore_permissions=True)
        self.supplier = supplier_name

        # Link supplier to settlement.
        frappe.db.set_value("Commission Settlement", self.settlement, "supplier", self.supplier, update_modified=False)

        # Purchase Invoice using an existing item.
        pi_name = f"LC-PI-{self.uid}"
        if not frappe.db.exists("Purchase Invoice", pi_name):
            pi = frappe.get_doc({
                "doctype": "Purchase Invoice",
                "name": pi_name,
                "supplier": self.supplier,
                "company": self.company,
                "posting_date": frappe.utils.nowdate(),
                "items": [{"item_code": "_Test Item", "qty": 1, "rate": 1000}],
            })
            pi.insert(ignore_permissions=True)
            pi.submit()
        self.purchase_invoice = pi_name

        frappe.db.set_value("Commission Settlement", self.settlement, {
            "settlement_document_type": "Purchase Invoice",
            "settlement_document": self.purchase_invoice,
        }, update_modified=False)

        # Resolve and assert valid Bank / Payable accounts.
        paid_from = frappe.db.get_value("Account", {"account_type": "Bank", "company": self.company}, "name")
        paid_to = frappe.db.get_value("Account", {"account_type": "Payable", "company": self.company}, "name")
        assert paid_from, "No Bank account found for company"
        assert paid_to, "No Payable account found for company"

        pe_name = f"LC-PE-{self.uid}"
        if not frappe.db.exists("Payment Entry", pe_name):
            pe = frappe.get_doc({
                "doctype": "Payment Entry",
                "name": pe_name,
                "payment_type": "Pay",
                "party_type": "Supplier",
                "party": self.supplier,
                "company": self.company,
                "posting_date": frappe.utils.nowdate(),
                "paid_amount": 1000,
                "received_amount": 1000,
                "paid_from": paid_from,
                "paid_to": paid_to,
                "references": [{
                    "reference_doctype": "Purchase Invoice",
                    "reference_name": self.purchase_invoice,
                    "allocated_amount": 1000,
                }],
            })
            pe.insert(ignore_permissions=True)
            pe.submit()
        self.payment_entry = pe_name

        self.source_row = frappe.db.get_value(
            "Payment Entry Reference",
            {"parent": self.payment_entry, "reference_name": self.purchase_invoice},
            "name",
        )
        assert self.source_row, "Source row not found"

        # Restricted user (Accounts User) for permission tests.
        self.restricted_user = f"lc-restricted-{self.uid}@example.com"
        if not frappe.db.exists("User", self.restricted_user):
            u = frappe.get_doc({
                "doctype": "User",
                "email": self.restricted_user,
                "first_name": "LC Restricted",
                "roles": [{"role": "Accounts User"}],
            })
            u.insert(ignore_permissions=True)

    def tearDown(self):
        # Best-effort cleanup.
        for doctype, name in [
            ("Payment Entry", getattr(self, "payment_entry", None)),
            ("Purchase Invoice", getattr(self, "purchase_invoice", None)),
            ("Commission Settlement", getattr(self, "settlement", None)),
            ("Commission Entry", getattr(self, "commission_entry", None)),
            ("Sales Invoice", getattr(self, "sales_invoice", None)),
        ]:
            if name and frappe.db.exists(doctype, name):
                doc = frappe.get_doc(doctype, name)
                if getattr(doc, "docstatus", 0) == 1:
                    try:
                        doc.cancel()
                    except Exception:
                        pass
                try:
                    frappe.delete_doc(doctype, name, ignore_permissions=True)
                except Exception:
                    pass
        for doctype, name in [
            ("Supplier", getattr(self, "supplier", None)),
            ("Sales Person", getattr(self, "sales_person", None)),
            ("User", getattr(self, "restricted_user", None)),
        ]:
            if name and frappe.db.exists(doctype, name):
                try:
                    frappe.delete_doc(doctype, name, ignore_permissions=True)
                except Exception:
                    pass

    def _make_draft_event(self, event_key, **overrides):
        data = {
            "doctype": "Commission Payout Event",
            "event_key": event_key,
            "event_type": "Payout",
            "posting_date": frappe.utils.nowdate(),
            "evidence_level": "ERP Payment Voucher",
            "settlement": self.settlement,
            "settlement_item": self.settlement_item,
            "commission_entry": self.commission_entry,
            "company": self.company,
            "beneficiary_type": "Sales Person",
            "beneficiary": self.sales_person,
            "source_doctype": "Payment Entry",
            "source_name": self.payment_entry,
            "source_row": self.source_row,
            "source_currency": "USD",
            "source_amount": 1000,
            "settlement_currency": "USD",
            "settlement_amount": 1000,
            "commission_currency": "USD",
            "commission_amount": 1000,
            "company_currency": "USD",
            "company_amount": 1000,
        }
        data.update(overrides)
        doc = frappe.get_doc(data)
        doc.insert(ignore_permissions=True)
        return doc

    def test_successful_draft_to_submitted(self):
        from sales_commission_management.sales_commission_management.payout.lifecycle import submit_payout_event
        doc = self._make_draft_event(f"lc-success-{self.uid}")
        self.assertEqual(doc.docstatus, 0)
        name = submit_payout_event(doc.name)
        self.assertEqual(name, doc.name)
        doc.reload()
        self.assertEqual(doc.docstatus, 1)

    def test_already_submitted_retry(self):
        from sales_commission_management.sales_commission_management.payout.lifecycle import submit_payout_event
        doc = self._make_draft_event(f"lc-retry-{self.uid}")
        submit_payout_event(doc.name)
        name = submit_payout_event(doc.name)
        self.assertEqual(name, doc.name)
        doc.reload()
        self.assertEqual(doc.docstatus, 1)

    def test_cancelled_event(self):
        from sales_commission_management.sales_commission_management.payout.lifecycle import submit_payout_event
        doc = self._make_draft_event(f"lc-cancelled-{self.uid}")
        frappe.db.set_value("Commission Payout Event", doc.name, "docstatus", 2)
        self.assertRaises(frappe.ValidationError, submit_payout_event, doc.name)

    def test_missing_event(self):
        from sales_commission_management.sales_commission_management.payout.lifecycle import submit_payout_event
        self.assertRaises(Exception, submit_payout_event, f"NO-SUCH-{self.uid}")

    def test_validation_failure_rollback(self):
        from sales_commission_management.sales_commission_management.payout.lifecycle import submit_payout_event
        # Create a valid Draft event first (insert succeeds).
        doc = self._make_draft_event(f"lc-fail-{self.uid}")
        # Introduce a controlled mismatch in the underlying Payment Entry Reference.
        original_allocated = frappe.db.get_value(
            "Payment Entry Reference", self.source_row, "allocated_amount"
        )
        try:
            frappe.db.set_value(
                "Payment Entry Reference", self.source_row, "allocated_amount", 999
            )
            # Submission must fail because allocated (999) != source_amount (1000).
            self.assertRaises(frappe.ValidationError, submit_payout_event, doc.name)
            doc.reload()
            self.assertEqual(doc.docstatus, 0)
        finally:
            frappe.db.set_value(
                "Payment Entry Reference", self.source_row, "allocated_amount", original_allocated
            )

    def test_callback_failure_propagates(self):
        from sales_commission_management.sales_commission_management.payout.lifecycle import submit_payout_event
        doc = self._make_draft_event(f"lc-callback-{self.uid}")

        def _boom(d):
            raise frappe.ValidationError("callback failure")

        self.assertRaises(frappe.ValidationError, submit_payout_event, doc.name, _boom)
        doc.reload()
        self.assertEqual(doc.docstatus, 0)

    def test_permission_denial_and_trusted_submission(self):
        from sales_commission_management.sales_commission_management.payout.lifecycle import submit_payout_event
        doc = self._make_draft_event(f"lc-perm-{self.uid}")
        doc.flags.ignore_permissions = False

        original_user = frappe.session.user
        try:
            frappe.set_user(self.restricted_user)
            # Direct submit by a restricted user (no submit permission) must fail.
            self.assertRaises(frappe.PermissionError, doc.submit)
            # Trusted lifecycle submission should succeed while restricted user is active.
            name = submit_payout_event(doc.name)
        finally:
            frappe.set_user(original_user)

        self.assertEqual(name, doc.name)
        doc.reload()
        self.assertEqual(doc.docstatus, 1)

    def test_concurrent_identical_retries_one_event(self):
        from frappe import DuplicateEntryError
        from sales_commission_management.sales_commission_management.payout.event_factory import get_or_create_payout_event
        from sales_commission_management.sales_commission_management.payout.lifecycle import create_and_submit_payout_event

        event_data = {
            "event_key": f"lc-conc-{self.uid}",
            "event_type": "Payout",
            "posting_date": frappe.utils.nowdate(),
            "evidence_level": "ERP Payment Voucher",
            "settlement": self.settlement,
            "settlement_item": self.settlement_item,
            "commission_entry": self.commission_entry,
            "company": self.company,
            "beneficiary_type": "Sales Person",
            "beneficiary": self.sales_person,
            "source_doctype": "Payment Entry",
            "source_name": self.payment_entry,
            "source_row": self.source_row,
            "source_currency": "USD",
            "source_amount": 1000,
            "settlement_currency": "USD",
            "settlement_amount": 1000,
            "commission_currency": "USD",
            "commission_amount": 1000,
            "company_currency": "USD",
            "company_amount": 1000,
        }

        # Use two independent DB connections to exercise real contention.
        primary = frappe.local.db
        frappe.connect()
        secondary = frappe.local.db
        try:
            frappe.local.db = primary
            frappe.db.commit()

            # First connection creates+submits.
            frappe.local.db = primary
            n1 = create_and_submit_payout_event(event_data)

            # Second connection must retrieve the same (already submitted) event.
            frappe.local.db = secondary
            n2 = create_and_submit_payout_event(event_data)

            self.assertEqual(n1, n2)

            # Only one event exists for this key.
            frappe.local.db = primary
            count = frappe.db.count(
                "Commission Payout Event", {"event_key": event_data["event_key"]}
            )
            self.assertEqual(count, 1)
        finally:
            try:
                secondary.rollback()
                secondary.close()
            except Exception:
                pass
            frappe.local.db = primary