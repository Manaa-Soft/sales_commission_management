from frappe.tests import IntegrationTestCase
import frappe
from frappe import _


class TestPayoutLifecycle(IntegrationTestCase):
    """Self-contained integration tests for the Commission Payout Event lifecycle.

    Covers creation, submission, retry, permissions, rollback and concurrency.
    All test records are created in setUp and cleaned up in tearDown.
    No dependency on pre-existing site data.
    """

    def setUp(self):
        import uuid

        self.uid = uuid.uuid4().hex[:8]

        # --- Company (chart of accounts is created by ERPNext on insert). ---
        company_name = f"Test Company {self.uid}"
        if not frappe.db.exists("Company", company_name):
            frappe.get_doc({
                "doctype": "Company",
                "company_name": company_name,
                "default_currency": "USD",
                "country": "United States",
            }).insert(ignore_permissions=True)
        self.company = company_name

        # --- Sales Person. ---
        sp_name = f"SP-{self.uid}"
        if not frappe.db.exists("Sales Person", sp_name):
            frappe.get_doc({
                "doctype": "Sales Person",
                "sales_person_name": sp_name,
            }).insert(ignore_permissions=True)
        self.sales_person = sp_name

        # --- Commission Settings (single doc) must exist before settlement. ---
        cs = frappe.get_single("Commission Settings")
        # Preserve original values for restoration in tearDown.
        self._original_cs_enabled = getattr(cs, "enabled", None)
        self._original_cs_accounting_mode = getattr(cs, "accounting_mode", None)
        cs.enabled = 1
        cs.accounting_mode = "Subledger Until Settlement"
        cs.save(ignore_permissions=True)
        self.commission_settings = cs

        # --- Item "Commission" (resolve/create and assert). Track creation. ---
        self._commission_item_created = not frappe.db.exists("Item", "Commission")
        if self._commission_item_created:
            frappe.get_doc({
                "doctype": "Item",
                "item_code": "Commission",
                "item_name": "Commission",
                "item_group": "All Item Groups",
                "stock_uom": "Nos",
                "is_stock_item": 0,
            }).insert(ignore_permissions=True)
        self.commission_item = frappe.db.get_value("Item", "Commission", "name")
        assert self.commission_item is not None, "Commission Item could not be resolved."

        # --- Customer + real submitted Sales Invoice for Commission Entry. ---
        customer_name = f"Test Customer {self.uid}"
        if not frappe.db.exists("Customer", customer_name):
            frappe.get_doc({
                "doctype": "Customer",
                "customer_name": customer_name,
                "customer_type": "Company",
            }).insert(ignore_permissions=True)
        self.customer = customer_name

        si_name = f"SI-TEST-{self.uid}"
        if not frappe.db.exists("Sales Invoice", si_name):
            si = frappe.get_doc({
                "doctype": "Sales Invoice",
                "name": si_name,
                "customer": self.customer,
                "company": self.company,
                "posting_date": frappe.utils.nowdate(),
                "items": [{"item_code": self.commission_item, "qty": 1, "rate": 1000}],
            })
            si.insert(ignore_permissions=True)
            si.submit()
        self.sales_invoice = si_name

        # --- Commission Entry referencing the real Sales Invoice. ---
        ce_name = f"CE-{self.uid}"
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
                "payable_amount": 1000,
                "collection_eligible_amount": 1000,
                "commission_currency": "USD",
                "company_currency": "USD",
                "status": "Earned",
                "docstatus": 1,
            }).insert(ignore_permissions=True)
        self.commission_entry = ce_name

        # --- Supplier created BEFORE settlement and linked to it. ---
        supplier_name = f"SUP-{self.uid}"
        if not frappe.db.exists("Supplier", supplier_name):
            frappe.get_doc({
                "doctype": "Supplier",
                "supplier_name": supplier_name,
                "supplier_type": "Company",
            }).insert(ignore_permissions=True)
        self.supplier = supplier_name

        # --- Commission Settlement. ---
        sett_name = f"CS-{self.uid}"
        if not frappe.db.exists("Commission Settlement", sett_name):
            sett = frappe.get_doc({
                "doctype": "Commission Settlement",
                "name": sett_name,
                "company": self.company,
                "beneficiary_type": "Sales Person",
                "sales_person": self.sales_person,
                "supplier": self.supplier,
                "status": "Draft",
                "commission_currency": "USD",
                "total_payable": 1000,
                "entries": [{
                    "commission_ledger_entry": self.commission_entry,
                    "amount_included": 1000,
                }],
            })
            sett.insert(ignore_permissions=True)
            sett.submit()
        else:
            # Ensure supplier linkage on pre-existing record.
            frappe.db.set_value("Commission Settlement", sett_name, "supplier", self.supplier, update_modified=False)
        self.settlement = sett_name

        # Ensure supplier linkage persists.
        frappe.db.set_value("Commission Settlement", self.settlement, "supplier", self.supplier, update_modified=False)

        self.settlement_item = frappe.db.get_value(
            "Commission Settlement Item",
            {"parent": self.settlement, "commission_ledger_entry": self.commission_entry},
            "name",
        )

        # --- Purchase Invoice for settlement document. ---
        pi_name = f"PI-{self.uid}"
        if not frappe.db.exists("Purchase Invoice", pi_name):
            pi = frappe.get_doc({
                "doctype": "Purchase Invoice",
                "name": pi_name,
                "supplier": self.supplier,
                "company": self.company,
                "posting_date": frappe.utils.nowdate(),
                "items": [{"item_code": self.commission_item, "qty": 1, "rate": 1000}],
            })
            pi.insert(ignore_permissions=True)
            pi.submit()
        self.purchase_invoice = pi_name

        frappe.db.set_value("Commission Settlement", self.settlement, {
            "settlement_document_type": "Purchase Invoice",
            "settlement_document": self.purchase_invoice,
        }, update_modified=False)

        # --- Payment Entry accounts: resolve and assert. ---
        bank_account = frappe.db.get_value("Account", {"account_type": "Bank", "company": self.company}, "name")
        payable_account = frappe.db.get_value("Account", {"account_type": "Payable", "company": self.company}, "name")
        assert bank_account is not None, "Bank account could not be resolved."
        assert payable_account is not None, "Payable account could not be resolved."
        self.bank_account = bank_account
        self.payable_account = payable_account

        # --- Payment Entry. ---
        pe_name = f"PE-{self.uid}"
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
                "paid_from": self.bank_account,
                "paid_to": self.payable_account,
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
        assert self.source_row is not None, "Payment Entry Reference (source row) could not be resolved."

    def _cleanup_uid_records(self, commit=False):
        """Idempotent cleanup of all committed records created by this test.

        Committed payout events (and any reversal/recovery events that
        reference them) are deleted FIRST, using the test-only direct SQL
        approach, so that link checks cannot block deletion of the linked
        fixtures. Linked fixtures are then removed in dependency order.
        """
        uid = getattr(self, "uid", None)

        # 1. Delete committed payout events first (test-only direct SQL).
        try:
            if uid:
                frappe.db.sql(
                    "DELETE FROM `tabCommission Payout Event` WHERE event_key LIKE %s",
                    (f"%{uid}%",),
                )
        except Exception:
            try:
                frappe.db.rollback()
            except Exception:
                pass

        # 2. Delete linked fixtures in dependency order.
        for doctype, attr in [
            ("Payment Entry", "payment_entry"),
            ("Purchase Invoice", "purchase_invoice"),
            ("Commission Settlement", "settlement"),
            ("Commission Entry", "commission_entry"),
            ("Sales Invoice", "sales_invoice"),
        ]:
            name = getattr(self, attr, None)
            if name and frappe.db.exists(doctype, name):
                try:
                    doc = frappe.get_doc(doctype, name)
                    if getattr(doc, "docstatus", 0) == 1:
                        try:
                            doc.cancel()
                        except Exception:
                            pass
                    frappe.delete_doc(doctype, name, ignore_permissions=True)
                except Exception:
                    pass

        for doctype, attr in [
            ("Supplier", "supplier"),
            ("Customer", "customer"),
            ("Sales Person", "sales_person"),
            ("Company", "company"),
        ]:
            name = getattr(self, attr, None)
            if name and frappe.db.exists(doctype, name):
                try:
                    frappe.delete_doc(doctype, name, ignore_permissions=True)
                except Exception:
                    pass

        # 3. Restore Commission Settings original values.
        try:
            cs = frappe.get_single("Commission Settings")
            if getattr(self, "_original_cs_enabled", None) is not None:
                cs.enabled = self._original_cs_enabled
            if getattr(self, "_original_cs_accounting_mode", None) is not None:
                cs.accounting_mode = self._original_cs_accounting_mode
            cs.save(ignore_permissions=True)
        except Exception:
            pass

        # 4. Remove Commission Item only if created by this test.
        if getattr(self, "_commission_item_created", False):
            try:
                if frappe.db.exists("Item", "Commission"):
                    frappe.delete_doc("Item", "Commission", ignore_permissions=True)
            except Exception:
                pass

        if commit:
            try:
                frappe.db.commit()
            except Exception:
                try:
                    frappe.db.rollback()
                except Exception:
                    pass

        # Do not commit here by default; let IntegrationTestCase handle rollback.
        # Concurrency tests that committed data should call frappe.db.commit()
        # explicitly after _cleanup_uid_records(commit=True).

    def tearDown(self):
        self._cleanup_uid_records()

    def _event_data(self, event_key, **overrides):
        data = {
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
        return data

    def _make_draft_event(self, event_key, **overrides):
        from sales_commission_management.sales_commission_management.payout.event_factory import get_or_create_payout_event

        name = get_or_create_payout_event(self._event_data(event_key, **overrides))
        return name

    # ------------------------------------------------------------------
    # Lifecycle tests
    # ------------------------------------------------------------------

    def test_successful_draft_to_submitted(self):
        from sales_commission_management.sales_commission_management.payout.lifecycle import submit_payout_event

        name = self._make_draft_event(f"lc-success-{self.uid}")
        doc = frappe.get_doc("Commission Payout Event", name)
        self.assertEqual(doc.docstatus, 0)
        returned = submit_payout_event(name)
        self.assertEqual(returned, name)
        doc.reload()
        self.assertEqual(doc.docstatus, 1)

    def test_already_submitted_retry(self):
        from sales_commission_management.sales_commission_management.payout.lifecycle import submit_payout_event

        name = self._make_draft_event(f"lc-retry-{self.uid}")
        submit_payout_event(name)
        # Retry must be idempotent.
        returned = submit_payout_event(name)
        self.assertEqual(returned, name)
        doc = frappe.get_doc("Commission Payout Event", name)
        self.assertEqual(doc.docstatus, 1)

    def test_cancelled_event_raises(self):
        from sales_commission_management.sales_commission_management.payout.lifecycle import submit_payout_event

        name = self._make_draft_event(f"lc-cancelled-{self.uid}")
        # Simulate cancellation inside the test transaction (no commit).
        frappe.db.set_value("Commission Payout Event", name, "docstatus", 2, update_modified=False)
        self.assertRaises(frappe.ValidationError, submit_payout_event, name)
        # Event remains cancelled; framework rolls back the transaction after test.

    def test_missing_event_raises(self):
        from sales_commission_management.sales_commission_management.payout.lifecycle import submit_payout_event

        self.assertRaises(Exception, submit_payout_event, "NONEXISTENT-EVENT-XYZ")

    def test_validation_failure(self):
        from sales_commission_management.sales_commission_management.payout.lifecycle import submit_payout_event

        name = self._make_draft_event(f"lc-validation-{self.uid}")

        def bad_validator(doc):
            frappe.throw(_("Source validation failed."))

        self.assertRaises(frappe.ValidationError, submit_payout_event, name, bad_validator)

    def test_callback_failure_propagates(self):
        from sales_commission_management.sales_commission_management.payout.lifecycle import submit_payout_event

        name = self._make_draft_event(f"lc-callback-{self.uid}")

        # A validator that raises a generic exception must propagate.
        def exploding_validator(doc):
            raise RuntimeError("boom")

        self.assertRaises(RuntimeError, submit_payout_event, name, exploding_validator)

    def test_create_and_submit_payout_event(self):
        from sales_commission_management.sales_commission_management.payout.lifecycle import create_and_submit_payout_event

        name = create_and_submit_payout_event(self._event_data(f"lc-create-submit-{self.uid}"))
        self.assertTrue(name)
        doc = frappe.get_doc("Commission Payout Event", name)
        self.assertEqual(doc.docstatus, 1)

    def test_restricted_user_direct_access_denied(self):
        from sales_commission_management.sales_commission_management.payout.lifecycle import submit_payout_event

        # Create a non-Administrator test user with a restricted role.
        restricted_user = f"restricted_{self.uid}@example.com"
        if not frappe.db.exists("User", restricted_user):
            u = frappe.get_doc({
                "doctype": "User",
                "email": restricted_user,
                "first_name": "Restricted",
                "enabled": 1,
                "roles": [{"role": "Accounts User"}],
            })
            u.insert(ignore_permissions=True)
            u.set_password("testpass123")
            u.save(ignore_permissions=True)

        # Switch to restricted user session.
        frappe.set_user(restricted_user)
        try:
            # Direct insert must be rejected (explicit DocType + permission error).
            data = {"doctype": "Commission Payout Event", **self._event_data(f"lc-restricted-direct-001-{self.uid}")}
            with self.assertRaises(frappe.PermissionError):
                frappe.get_doc(data).insert()
            # Direct submit of an existing draft must be rejected.
            name = None
            frappe.set_user("Administrator")
            name = self._make_draft_event(f"lc-restricted-direct-002-{self.uid}")
            frappe.set_user(restricted_user)
            doc = frappe.get_doc("Commission Payout Event", name)
            with self.assertRaises(frappe.PermissionError):
                doc.submit()

            # Trusted lifecycle handler must work under the restricted-user session.
            frappe.set_user("Administrator")
            trusted_name = self._make_draft_event(f"lc-restricted-trusted-session-001-{self.uid}")
            frappe.set_user(restricted_user)
            from sales_commission_management.sales_commission_management.payout.lifecycle import submit_payout_event
            returned = submit_payout_event(trusted_name)
            self.assertEqual(returned, trusted_name)
            trusted_doc = frappe.get_doc("Commission Payout Event", trusted_name)
            self.assertEqual(trusted_doc.docstatus, 1)
        finally:
            frappe.set_user("Administrator")

    def test_trusted_submission_restores_permissions(self):
        from sales_commission_management.sales_commission_management.payout.lifecycle import submit_payout_event

        name = self._make_draft_event(f"lc-restricted-trusted-001-{self.uid}")
        # Capture flags before.
        prev_doc_flag_before = bool(getattr(frappe.get_doc("Commission Payout Event", name).flags, "ignore_permissions", False))
        prev_req_flag_before = bool(getattr(frappe.flags, "ignore_permissions", False))

        # Successful submission.
        returned = submit_payout_event(name)
        self.assertEqual(returned, name)
        doc = frappe.get_doc("Commission Payout Event", name)
        self.assertEqual(doc.docstatus, 1)
        # Flags restored after success.
        self.assertEqual(bool(getattr(doc.flags, "ignore_permissions", False)), prev_doc_flag_before)
        self.assertEqual(bool(getattr(frappe.flags, "ignore_permissions", False)), prev_req_flag_before)

        # Failed submission restores flags too.
        name2 = self._make_draft_event(f"lc-restricted-trusted-002-{self.uid}")

        def bad_validator(d):
            # Trusted handler must have elevated permissions on the actual doc.
            self.assertTrue(getattr(d.flags, "ignore_permissions", False))
            frappe.throw(_("boom"))

        with self.assertRaises(frappe.ValidationError):
            submit_payout_event(name2, bad_validator)
        doc2 = frappe.get_doc("Commission Payout Event", name2)
        self.assertEqual(doc2.docstatus, 0)
        self.assertEqual(bool(getattr(doc2.flags, "ignore_permissions", False)), False)
        self.assertEqual(bool(getattr(frappe.flags, "ignore_permissions", False)), prev_req_flag_before)

    def test_source_transaction_and_event_rollback(self):
        from sales_commission_management.sales_commission_management.payout.lifecycle import (
            create_and_submit_payout_event,
        )

        event_key = f"lc-rollback-{self.uid}"
        original_remarks = frappe.db.get_value("Payment Entry", self.payment_entry, "remarks")

        sp = "lc_rollback_test"
        frappe.db.savepoint(sp)
        try:
            # Perform a representative source-side database change.
            frappe.db.set_value(
                "Payment Entry", self.payment_entry, "remarks", "ROLLBACK-TEST-MARKER", update_modified=False
            )

            event_data = self._event_data(event_key)

            def validator_that_fails_after_source_change(doc):
                frappe.throw(_("Source transaction invalid."))

            # create_and_submit_payout_event creates (or retrieves) the event and
            # then submits it. The validator fails after the source-side change,
            # so the whole operation must be rolled back by the caller.
            try:
                create_and_submit_payout_event(event_data, validator_that_fails_after_source_change)
            except frappe.ValidationError:
                pass

            # Undo the source-side change AND any event creation/submission
            # without rolling back the whole test transaction.
            frappe.db.rollback(save_point=sp)

            # 1. The Payment Entry change is rolled back.
            current_remarks = frappe.db.get_value("Payment Entry", self.payment_entry, "remarks")
            self.assertEqual(current_remarks, original_remarks)

            # 2. The newly created payout event does not exist after rollback.
            event_name_for_key = frappe.db.get_value(
                "Commission Payout Event", {"event_key": event_key}, "name"
            )
            self.assertIsNone(event_name_for_key)

            # 3. No submitted payout event remains for the event key.
            submitted = frappe.db.get_value(
                "Commission Payout Event", {"event_key": event_key, "docstatus": 1}, "name"
            )
            self.assertIsNone(submitted)
        finally:
            try:
                frappe.db.rollback(save_point=sp)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # MariaDB concurrency tests (genuinely concurrent via worker threads)
    # ------------------------------------------------------------------
    def test_concurrent_event_creation_mariadb(self):
        if frappe.db.db_type != "mariadb":
            self.skipTest("MariaDB locking reads required for concurrency test")

        import threading

        from sales_commission_management.sales_commission_management.payout.event_factory import get_or_create_payout_event

        event_key = f"lc-concurrent-create-{self.uid}"
        event_data = self._event_data(event_key)

        # Commit baseline fixtures so worker connections can see them.
        frappe.db.commit()

        site = frappe.local.site
        primary = frappe.local.db

        results = [None, None]
        errors = [None, None]
        ready_barrier = threading.Barrier(2)
        ready_event = threading.Event()
        start_event = threading.Event()
        lookup_barrier = threading.Barrier(2)

        def worker(idx):
            try:
                frappe.init(site=site)
                frappe.connect()
                frappe.set_user("Administrator")
                ready_barrier.wait(timeout=30)
                ready_event.set()
                start_event.wait(timeout=30)
                name = get_or_create_payout_event(dict(event_data), lookup_barrier=lookup_barrier)
                frappe.db.commit()
                results[idx] = name
            except Exception as exc:  # noqa: BLE001
                errors[idx] = exc
                try:
                    frappe.db.rollback()
                except Exception:
                    pass
            finally:
                try:
                    frappe.local.db.close()
                except Exception:
                    pass

        t0 = threading.Thread(target=worker, args=(0,), daemon=True)
        t1 = threading.Thread(target=worker, args=(1,), daemon=True)
        t0.start()
        t1.start()
        try:
            if not ready_event.wait(timeout=30):
                raise RuntimeError("Worker threads did not become ready")
            start_event.set()
            t0.join(timeout=60)
            t1.join(timeout=60)
            self.assertFalse(t0.is_alive(), "concurrent creation worker 0 did not terminate")
            self.assertFalse(t1.is_alive(), "concurrent creation worker 1 did not terminate")

            frappe.local.db = primary

            for err in errors:
                if err is not None:
                    raise err

            self.assertIsNotNone(results[0])
            self.assertIsNotNone(results[1])
            self.assertEqual(results[0], results[1])

            count = frappe.db.count("Commission Payout Event", {"event_key": event_key})
            self.assertEqual(count, 1)

            doc = frappe.get_doc("Commission Payout Event", results[0])
            self.assertEqual(doc.source_amount, event_data["source_amount"])
            self.assertEqual(doc.settlement_amount, event_data["settlement_amount"])
            self.assertEqual(doc.commission_amount, event_data["commission_amount"])
            self.assertEqual(doc.company_amount, event_data["company_amount"])
            self.assertEqual(doc.source_currency, event_data["source_currency"])
        finally:
            start_event.set()
            t0.join(timeout=30)
            t1.join(timeout=30)
            frappe.local.db = primary
            self.assertFalse(t0.is_alive(), "concurrent creation worker 0 did not terminate")
            self.assertFalse(t1.is_alive(), "concurrent creation worker 1 did not terminate")
            try:
                frappe.db.sql("DELETE FROM `tabCommission Payout Event` WHERE event_key = %s", (event_key,))
                frappe.db.commit()
            except Exception:
                try:
                    frappe.db.rollback()
                except Exception:
                    pass
            # Ensure committed fixtures are removed (test-only).
            try:
                self._cleanup_uid_records(commit=True)
            except Exception:
                try:
                    frappe.db.rollback()
                except Exception:
                    pass
            # Assert no UID-scoped records remain after cleanup.
            remaining_events = frappe.db.sql(
                "SELECT COUNT(*) FROM `tabCommission Payout Event` WHERE event_key LIKE %s",
                (f"%{self.uid}%",),
            )[0][0]
            self.assertEqual(remaining_events, 0)
            for doctype, attr in [
                ("Payment Entry", "payment_entry"),
                ("Purchase Invoice", "purchase_invoice"),
                ("Commission Settlement", "settlement"),
                ("Commission Entry", "commission_entry"),
                ("Sales Invoice", "sales_invoice"),
                ("Supplier", "supplier"),
                ("Customer", "customer"),
                ("Sales Person", "sales_person"),
                ("Company", "company"),
            ]:
                name = getattr(self, attr, None)
                if name:
                    self.assertFalse(
                        frappe.db.exists(doctype, name),
                        f"{doctype} {name} remains after cleanup",
                    )

    def test_concurrent_reversal_mariadb(self):
        if frappe.db.db_type != "mariadb":
            self.skipTest("MariaDB locking reads required for concurrency test")

        import threading

        from frappe.utils import flt
        from sales_commission_management.sales_commission_management.payout.lifecycle import create_and_submit_payout_event

        # Create and submit an original payout event.
        orig_name = self._make_submitted_payout_event(f"lc-concurrent-rev-orig-{self.uid}")
        frappe.db.commit()

        site = frappe.local.site
        primary = frappe.local.db

        rev_data_a = self._event_data(f"lc-concurrent-rev-a-{self.uid}")
        rev_data_a["event_type"] = "Payout Reversal"
        rev_data_a["reversal_of"] = orig_name
        rev_data_a["commission_amount"] = 600
        rev_data_a["settlement_amount"] = 600
        rev_data_a["company_amount"] = 600

        rev_data_b = self._event_data(f"lc-concurrent-rev-b-{self.uid}")
        rev_data_b["event_type"] = "Payout Reversal"
        rev_data_b["reversal_of"] = orig_name
        rev_data_b["commission_amount"] = 600
        rev_data_b["settlement_amount"] = 600
        rev_data_b["company_amount"] = 600

        results = [None, None]
        ready_barrier = threading.Barrier(2)
        ready_event = threading.Event()
        start_event = threading.Event()

        def worker(idx, rev_data):
            try:
                frappe.init(site=site)
                frappe.connect()
                frappe.set_user("Administrator")
                ready_barrier.wait(timeout=30)
                ready_event.set()
                start_event.wait(timeout=30)
                name = create_and_submit_payout_event(dict(rev_data))
                frappe.db.commit()
                results[idx] = ("ok", name)
            except Exception as exc:  # noqa: BLE001
                try:
                    frappe.db.rollback()
                except Exception:
                    pass
                results[idx] = ("err", exc)
            finally:
                try:
                    frappe.local.db.close()
                except Exception:
                    pass

        t0 = threading.Thread(target=worker, args=(0, rev_data_a), daemon=True)
        t1 = threading.Thread(target=worker, args=(1, rev_data_b), daemon=True)
        t0.start()
        t1.start()
        try:
            if not ready_event.wait(timeout=30):
                raise RuntimeError("Worker threads did not become ready")
            start_event.set()
            t0.join(timeout=60)
            t1.join(timeout=60)
            self.assertFalse(t0.is_alive(), "concurrent reversal worker 0 did not terminate")
            self.assertFalse(t1.is_alive(), "concurrent reversal worker 1 did not terminate")

            frappe.local.db = primary

            statuses = [r[0] for r in results if r is not None]
            self.assertEqual(sorted(statuses), ["err", "ok"])

            ok_result = [r for r in results if r and r[0] == "ok"][0]
            err_result = [r for r in results if r and r[0] == "err"][0]
            self.assertTrue(isinstance(err_result[1], frappe.ValidationError))
            self.assertIn("Cumulative reversal exceeds original event amount.", str(err_result[1]))

            reversals = frappe.db.get_all(
                "Commission Payout Event",
                filters={"reversal_of": orig_name, "docstatus": 1},
                pluck="name",
            )
            self.assertEqual(len(reversals), 1)
            self.assertEqual(reversals[0], ok_result[1])

            total_comm = frappe.db.sql(
                "SELECT COALESCE(SUM(commission_amount), 0) FROM `tabCommission Payout Event` "
                "WHERE reversal_of = %s AND docstatus = 1",
                (orig_name,),
            )[0][0]
            total_sett = frappe.db.sql(
                "SELECT COALESCE(SUM(settlement_amount), 0) FROM `tabCommission Payout Event` "
                "WHERE reversal_of = %s AND docstatus = 1",
                (orig_name,),
            )[0][0]
            total_comp = frappe.db.sql(
                "SELECT COALESCE(SUM(company_amount), 0) FROM `tabCommission Payout Event` "
                "WHERE reversal_of = %s AND docstatus = 1",
                (orig_name,),
            )[0][0]
            self.assertEqual(flt(total_comm), 600)
            self.assertEqual(flt(total_sett), 600)
            self.assertEqual(flt(total_comp), 600)
        finally:
            start_event.set()
            t0.join(timeout=30)
            t1.join(timeout=30)
            frappe.local.db = primary
            self.assertFalse(t0.is_alive(), "concurrent reversal worker 0 did not terminate")
            self.assertFalse(t1.is_alive(), "concurrent reversal worker 1 did not terminate")
            try:
                frappe.db.sql("DELETE FROM `tabCommission Payout Event` WHERE name = %s", (orig_name,))
                frappe.db.sql("DELETE FROM `tabCommission Payout Event` WHERE reversal_of = %s", (orig_name,))
                frappe.db.commit()
            except Exception:
                try:
                    frappe.db.rollback()
                except Exception:
                    pass
            # Ensure committed fixtures are removed (test-only).
            try:
                self._cleanup_uid_records(commit=True)
            except Exception:
                try:
                    frappe.db.rollback()
                except Exception:
                    pass
            # Assert no UID-scoped records remain after cleanup.
            remaining_events = frappe.db.sql(
                "SELECT COUNT(*) FROM `tabCommission Payout Event` WHERE event_key LIKE %s",
                (f"%{self.uid}%",),
            )[0][0]
            self.assertEqual(remaining_events, 0)
            for doctype, attr in [
                ("Payment Entry", "payment_entry"),
                ("Purchase Invoice", "purchase_invoice"),
                ("Commission Settlement", "settlement"),
                ("Commission Entry", "commission_entry"),
                ("Sales Invoice", "sales_invoice"),
                ("Supplier", "supplier"),
                ("Customer", "customer"),
                ("Sales Person", "sales_person"),
                ("Company", "company"),
            ]:
                name = getattr(self, attr, None)
                if name:
                    self.assertFalse(
                        frappe.db.exists(doctype, name),
                        f"{doctype} {name} remains after cleanup",
                    )

    # ------------------------------------------------------------------
    # Reversal amount / precision tests
    # ------------------------------------------------------------------
    def _make_submitted_payout_event(self, event_key):
        from sales_commission_management.sales_commission_management.payout.lifecycle import create_and_submit_payout_event

        name = create_and_submit_payout_event(self._event_data(event_key))
        return name

    def test_reversal_negative_settlement_amount_rejected(self):
        from sales_commission_management.sales_commission_management.payout.event_factory import get_or_create_payout_event

        orig_name = self._make_submitted_payout_event(f'lc-rev-neg-sett-001-{self.uid}')
        data = self._event_data(f'lc-rev-neg-sett-002-{self.uid}')
        data['event_type'] = 'Payout Reversal'
        data['reversal_of'] = orig_name
        data['commission_amount'] = 1000
        data['settlement_amount'] = -100
        data['company_amount'] = 1000
        self.assertRaises(frappe.ValidationError, get_or_create_payout_event, data)

    def test_reversal_zero_company_amount_rejected(self):
        from sales_commission_management.sales_commission_management.payout.event_factory import get_or_create_payout_event

        orig_name = self._make_submitted_payout_event(f'lc-rev-zero-comp-001-{self.uid}')
        data = self._event_data(f'lc-rev-zero-comp-002-{self.uid}')
        data['event_type'] = 'Payout Reversal'
        data['reversal_of'] = orig_name
        data['commission_amount'] = 1000
        data['settlement_amount'] = 1000
        data['company_amount'] = 0
        self.assertRaises(frappe.ValidationError, get_or_create_payout_event, data)

    def test_reversal_zero_commission_amount_rejected(self):
        from sales_commission_management.sales_commission_management.payout.event_factory import get_or_create_payout_event

        orig_name = self._make_submitted_payout_event(f'lc-rev-zero-comm-001-{self.uid}')
        data = self._event_data(f'lc-rev-zero-comm-002-{self.uid}')
        data['event_type'] = 'Payout Reversal'
        data['reversal_of'] = orig_name
        data['commission_amount'] = 0
        data['settlement_amount'] = 1000
        data['company_amount'] = 1000
        self.assertRaises(frappe.ValidationError, get_or_create_payout_event, data)

    def test_reversal_fractional_rounds_to_zero_rejected(self):
        from sales_commission_management.sales_commission_management.payout.event_factory import get_or_create_payout_event

        orig_name = self._make_submitted_payout_event(f'lc-rev-frac-001-{self.uid}')
        data = self._event_data(f'lc-rev-frac-002-{self.uid}')
        data['event_type'] = 'Payout Reversal'
        data['reversal_of'] = orig_name
        data['commission_amount'] = 0.004
        data['settlement_amount'] = 0.004
        data['company_amount'] = 0.004
        self.assertRaises(frappe.ValidationError, get_or_create_payout_event, data)

    def test_reversal_negative_amounts_rejected(self):
        from sales_commission_management.sales_commission_management.payout.event_factory import get_or_create_payout_event

        orig_name = self._make_submitted_payout_event(f'lc-rev-neg-all-001-{self.uid}')
        data = self._event_data(f'lc-rev-neg-all-002-{self.uid}')
        data['event_type'] = 'Payout Reversal'
        data['reversal_of'] = orig_name
        data['commission_amount'] = -1
        data['settlement_amount'] = -1
        data['company_amount'] = -1
        self.assertRaises(frappe.ValidationError, get_or_create_payout_event, data)
