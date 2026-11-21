from frappe.tests import IntegrationTestCase

import frappe

from sales_commission_management.sales_commission_management.payout.supplier_payment import (
    allocate_supplier_payment_across_items,
    get_supplier_settlement_for_reference,
)


class TestSupplierPaymentAllocation(IntegrationTestCase):
    def test_proportional_allocation(self):
        items = [
            {"item": "A", "settlement_amount": 100, "previously_paid": 0},
            {"item": "B", "settlement_amount": 200, "previously_paid": 0},
        ]
        res = allocate_supplier_payment_across_items(
            payment_allocation=90,
            source_allocation=90,
            target_exchange_rate=1,
            settlement_fx_rate=1,
            items=items,
        )
        # 1/3 and 2/3
        self.assertEqual(len(res), 2)
        total = sum(r["allocation"] for r in res)
        self.assertAlmostEqual(total, 90, places=2)

    def test_reject_over_allocation(self):
        items = [{"item": "A", "settlement_amount": 50, "previously_paid": 0}]
        try:
            allocate_supplier_payment_across_items(
                payment_allocation=60,
                source_allocation=60,
                target_exchange_rate=1,
                settlement_fx_rate=1,
                items=items,
            )
            self.fail("Expected ValueError")
        except ValueError:
            pass

    def test_deterministic_rounding(self):
        items = [
            {"item": "A", "settlement_amount": 100, "previously_paid": 0},
            {"item": "B", "settlement_amount": 100, "previously_paid": 0},
            {"item": "C", "settlement_amount": 100, "previously_paid": 0},
        ]
        r1 = allocate_supplier_payment_across_items(
            payment_allocation=100,
            source_allocation=100,
            target_exchange_rate=1,
            settlement_fx_rate=1,
            items=items,
        )
        r2 = allocate_supplier_payment_across_items(
            payment_allocation=100,
            source_allocation=100,
            target_exchange_rate=1,
            settlement_fx_rate=1,
            items=items,
        )
        self.assertEqual(r1, r2)

    def test_explicit_target_rate_zero_rejected(self):
        items = [{"item": "A", "settlement_amount": 100, "previously_paid": 0}]
        with self.assertRaises(ValueError):
            allocate_supplier_payment_across_items(
                payment_allocation=10,
                source_allocation=10,
                target_exchange_rate=0,
                settlement_fx_rate=1,
                items=items,
            )

    def test_explicit_settlement_fx_zero_rejected(self):
        items = [{"item": "A", "settlement_amount": 100, "previously_paid": 0}]
        with self.assertRaises(ValueError):
            allocate_supplier_payment_across_items(
                payment_allocation=10,
                source_allocation=10,
                target_exchange_rate=1,
                settlement_fx_rate=0,
                items=items,
            )

    def test_missing_exchange_rates_rejected(self):
        items = [{"item": "A", "settlement_amount": 100, "previously_paid": 0}]
        with self.assertRaises(ValueError):
            allocate_supplier_payment_across_items(
                payment_allocation=10,
                source_allocation=10,
                items=items,
            )

    def test_multi_currency_minor_unit_reconciles(self):
        items = [
            {"item": "A", "settlement_amount": 0.02, "previously_paid": 0},
            {"item": "B", "settlement_amount": 0.01, "previously_paid": 0},
        ]
        res = allocate_supplier_payment_across_items(
            payment_allocation=0.01,
            source_allocation=0.02,
            target_exchange_rate=1,
            settlement_fx_rate=1,
            items=items,
        )
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["item"], "A")
        self.assertAlmostEqual(res[0]["settlement_amount"], 0.01, places=2)
        self.assertAlmostEqual(res[0]["source_amount"], 0.02, places=2)
        total_sett = sum(r["settlement_amount"] for r in res)
        total_src = sum(r["source_amount"] for r in res)
        self.assertAlmostEqual(total_sett, 0.01, places=2)
        self.assertAlmostEqual(total_src, 0.02, places=2)

    def test_zero_share_settlement_no_lost_allocations(self):
        items = [
            {"item": "A", "settlement_amount": 0.02, "previously_paid": 0},
            {"item": "B", "settlement_amount": 0.01, "previously_paid": 0},
        ]
        res = allocate_supplier_payment_across_items(
            payment_allocation=0.01,
            source_allocation=0.02,
            target_exchange_rate=1,
            settlement_fx_rate=1,
            items=items,
        )
        # Item B receives no settlement allocation and is excluded, but the
        # source total is still fully allocated to eligible items.
        self.assertEqual(len(res), 1)
        total_src = sum(r["source_amount"] for r in res)
        self.assertAlmostEqual(total_src, 0.02, places=2)

    def test_three_equal_items_minor_distribution(self):
        items = [
            {"item": "A", "settlement_amount": 100, "previously_paid": 0},
            {"item": "B", "settlement_amount": 100, "previously_paid": 0},
            {"item": "C", "settlement_amount": 100, "previously_paid": 0},
        ]
        res = allocate_supplier_payment_across_items(
            payment_allocation=100,
            source_allocation=100,
            target_exchange_rate=1,
            settlement_fx_rate=1,
            items=items,
        )
        allocs = sorted(r["allocation"] for r in res)
        # 10000 minor / 3 = 3333.333; largest remainder gives 33.34,33.33,33.33
        self.assertEqual(allocs, [33.33, 33.33, 33.34])

    def test_reference_list_empty_rejected(self):
        pe = {
            "name": "PE-1",
            "docstatus": 1,
            "payment_type": "Pay",
            "party_type": "Supplier",
            "company": "C",
            "party": "S",
            "references": [],
        }
        pr = {
            "parent": "PE-1",
            "reference_doctype": "Purchase Invoice",
            "reference_name": "PI-1",
            "allocated_amount": 10,
        }
        self.assertIsNone(get_supplier_settlement_for_reference(pe, pr))

    def test_matching_child_name_altered_fields_rejected(self):
        pe = {
            "name": "PE-1",
            "docstatus": 1,
            "payment_type": "Pay",
            "party_type": "Supplier",
            "company": "C",
            "party": "S",
            "references": [
                {
                    "name": "REF-1",
                    "reference_doctype": "Purchase Invoice",
                    "reference_name": "PI-1",
                    "allocated_amount": 10,
                }
            ],
        }
        pr = {
            "name": "REF-1",
            "parent": "PE-1",
            "reference_doctype": "Purchase Invoice",
            "reference_name": "PI-1",
            "allocated_amount": 999,
        }
        self.assertIsNone(get_supplier_settlement_for_reference(pe, pr))

    def test_reference_from_another_payment_entry_rejected(self):
        pe = {
            "name": "PE-1",
            "docstatus": 1,
            "payment_type": "Pay",
            "party_type": "Supplier",
            "company": "C",
            "party": "S",
            "references": [],
        }
        pr = {
            "parent": "OTHER-PE",
            "reference_doctype": "Purchase Invoice",
            "reference_name": "PI-1",
            "allocated_amount": 10,
        }
        self.assertIsNone(get_supplier_settlement_for_reference(pe, pr))

    def test_amount_included_converted_through_fx_snapshot(self):
        items = [{"item": "A", "amount_included": 100, "previously_paid": 0}]
        res = allocate_supplier_payment_across_items(
            payment_allocation=200,
            source_allocation=200,
            target_exchange_rate=1,
            settlement_fx_rate=2,
            items=items,
        )
        self.assertEqual(len(res), 1)
        self.assertAlmostEqual(res[0]["settlement_amount"], 200, places=2)
        self.assertAlmostEqual(res[0]["commission_amount"], 100, places=2)

    def test_previous_partial_payment_allocates_outstanding(self):
        items = [{"item": "A", "settlement_amount": 100, "previously_paid": 40}]
        res = allocate_supplier_payment_across_items(
            payment_allocation=60,
            source_allocation=60,
            target_exchange_rate=1,
            settlement_fx_rate=1,
            items=items,
        )
        self.assertEqual(len(res), 1)
        self.assertAlmostEqual(res[0]["allocation"], 60, places=2)

    def test_zero_commission_allocation_rejected(self):
        # Three items each 0.01 commission (comm currency), FX rate 3 so settlement
        # 0.03 each. Payment 0.03 settlement -> commission total 0.01, which
        # cannot be split across three items at 2-decimal precision.
        items = [
            {"item": "A", "settlement_amount": 0.01, "previously_paid": 0},
            {"item": "B", "settlement_amount": 0.01, "previously_paid": 0},
            {"item": "C", "settlement_amount": 0.01, "previously_paid": 0},
        ]
        with self.assertRaises(ValueError):
            allocate_supplier_payment_across_items(
                payment_allocation=0.03,
                source_allocation=0.03,
                target_exchange_rate=1,
                settlement_fx_rate=3,
                items=items,
            )

    def test_negative_nan_infinite_rejected(self):
        items = [{"item": "A", "settlement_amount": 100, "previously_paid": 0}]
        for bad in (-1, float("nan"), float("inf"), float("-inf")):
            with self.assertRaises(ValueError):
                allocate_supplier_payment_across_items(
                    payment_allocation=bad if bad != -1 else -5,
                    source_allocation=10,
                    target_exchange_rate=1,
                    settlement_fx_rate=1,
                    items=items,
                )
        with self.assertRaises(ValueError):
            allocate_supplier_payment_across_items(
                payment_allocation=10,
                source_allocation=10,
                target_exchange_rate=-1,
                settlement_fx_rate=1,
                items=items,
            )
        with self.assertRaises(ValueError):
            allocate_supplier_payment_across_items(
                payment_allocation=10,
                source_allocation=10,
                target_exchange_rate=1,
                settlement_fx_rate=float("nan"),
                items=items,
            )


# ---------------------------------------------------------------------------
# P3.2.2 — Supplier Payment Entry handler integration tests
# ---------------------------------------------------------------------------


class TestSupplierPaymentHandler(IntegrationTestCase):
    """Integration tests for process_supplier_payment_entry() (P3.2.2)."""

    def setUp(self):
        import uuid

        self.uid = uuid.uuid4().hex[:8]

        company_name = f"Test Company {self.uid}"
        if not frappe.db.exists("Company", company_name):
            frappe.get_doc({
                "doctype": "Company",
                "company_name": company_name,
                "default_currency": "USD",
                "country": "United States",
            }).insert(ignore_permissions=True)
        self.company = company_name

        sp_name = f"SP-{self.uid}"
        if not frappe.db.exists("Sales Person", sp_name):
            frappe.get_doc({
                "doctype": "Sales Person",
                "sales_person_name": sp_name,
            }).insert(ignore_permissions=True)
        self.sales_person = sp_name

        cs = frappe.get_single("Commission Settings")
        self._original_cs_enabled = getattr(cs, "enabled", None)
        self._original_cs_accounting_mode = getattr(cs, "accounting_mode", None)
        cs.enabled = 1
        cs.accounting_mode = "Subledger Until Settlement"
        cs.save(ignore_permissions=True)

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

        supplier_name = f"SUP-{self.uid}"
        if not frappe.db.exists("Supplier", supplier_name):
            frappe.get_doc({
                "doctype": "Supplier",
                "supplier_name": supplier_name,
                "supplier_type": "Company",
            }).insert(ignore_permissions=True)
        self.supplier = supplier_name

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
                "settlement_method": "Supplier Purchase Invoice",
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
            frappe.db.set_value("Commission Settlement", sett_name, "supplier", self.supplier, update_modified=False)
            frappe.db.set_value("Commission Settlement", sett_name, "settlement_method", "Supplier Purchase Invoice", update_modified=False)
        self.settlement = sett_name
        frappe.db.set_value("Commission Settlement", self.settlement, "supplier", self.supplier, update_modified=False)
        frappe.db.set_value("Commission Settlement", self.settlement, "settlement_method", "Supplier Purchase Invoice", update_modified=False)

        self.settlement_item = frappe.db.get_value(
            "Commission Settlement Item",
            {"parent": self.settlement, "commission_ledger_entry": self.commission_entry},
            "name",
        )

        pi_name = f"PI-{self.uid}"
        if not frappe.db.exists("Purchase Invoice", pi_name):
            pi = frappe.get_doc({
                "doctype": "Purchase Invoice",
                "name": pi_name,
                "supplier": self.supplier,
                "company": self.company,
                "posting_date": frappe.utils.nowdate(),
                "items": [{"item_code": self.commission_item, "qty": 1, "rate": 2000}],
            })
            pi.insert(ignore_permissions=True)
            pi.submit()
        self.purchase_invoice = pi_name

        frappe.db.set_value("Commission Settlement", self.settlement, {
            "settlement_document_type": "Purchase Invoice",
            "settlement_document": self.purchase_invoice,
            "settlement_method": "Supplier Purchase Invoice",
        }, update_modified=False)

        bank_account = frappe.db.get_value("Account", {"account_type": "Bank", "company": self.company}, "name")
        payable_account = frappe.db.get_value("Account", {"account_type": "Payable", "company": self.company}, "name")
        self.bank_account = bank_account
        self.payable_account = payable_account

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
                "target_exchange_rate": 1,
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

    def _cleanup_uid_records(self, commit=False):
        uid = getattr(self, "uid", None)
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

        # second payment entry if created
        pe2 = getattr(self, "payment_entry_2", None)
        if pe2 and frappe.db.exists("Payment Entry", pe2):
            try:
                doc = frappe.get_doc("Payment Entry", pe2)
                if getattr(doc, "docstatus", 0) == 1:
                    try:
                        doc.cancel()
                    except Exception:
                        pass
                frappe.delete_doc("Payment Entry", pe2, ignore_permissions=True)
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

        try:
            cs = frappe.get_single("Commission Settings")
            if getattr(self, "_original_cs_enabled", None) is not None:
                cs.enabled = self._original_cs_enabled
            if getattr(self, "_original_cs_accounting_mode", None) is not None:
                cs.accounting_mode = self._original_cs_accounting_mode
            cs.save(ignore_permissions=True)
        except Exception:
            pass

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

    def tearDown(self):
        self._cleanup_uid_records()

    # ------------------------------------------------------------------
    # Test 3: Multiple settlements matched to one PI -> validation error
    # ------------------------------------------------------------------
    def test_multiple_settlements_same_pi_raises(self):
        from sales_commission_management.sales_commission_management.payout.supplier_payment import (
            process_supplier_payment_entry,
        )

        # Create a second submitted settlement linked to the same PI.
        sett2_name = f"CS2-{self.uid}"
        if not frappe.db.exists("Commission Settlement", sett2_name):
            sett2 = frappe.get_doc({
                "doctype": "Commission Settlement",
                "name": sett2_name,
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
            sett2.insert(ignore_permissions=True)
            sett2.submit()
        frappe.db.set_value("Commission Settlement", sett2_name, {
            "settlement_document_type": "Purchase Invoice",
            "settlement_document": self.purchase_invoice,
            "settlement_method": "Supplier Purchase Invoice",
        }, update_modified=False)
        # Ensure supplier linkage.
        frappe.db.set_value("Commission Settlement", sett2_name, "supplier", self.supplier, update_modified=False)

        try:
            with self.assertRaises(frappe.ValidationError) as ctx:
                process_supplier_payment_entry(self.payment_entry)
            self.assertIn("Multiple submitted Commission Settlements", str(ctx.exception))
        finally:
            # cleanup second settlement
            try:
                if frappe.db.exists("Commission Settlement", sett2_name):
                    d = frappe.get_doc("Commission Settlement", sett2_name)
                    if getattr(d, "docstatus", 0) == 1:
                        d.cancel()
                    frappe.delete_doc("Commission Settlement", sett2_name, ignore_permissions=True)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Test 4: Database failure during settlement matching -> transaction fails
    # ------------------------------------------------------------------
    def test_db_failure_during_settlement_matching_fails(self):
        from sales_commission_management.sales_commission_management.payout.supplier_payment import (
            process_supplier_payment_entry,
        )

        import sales_commission_management.sales_commission_management.payout.supplier_payment as sp_mod

        original_get_all = frappe.get_all

        def boom(*a, **k):
            raise RuntimeError("simulated db failure")

        frappe.get_all = boom
        try:
            with self.assertRaises(RuntimeError) as ctx:
                process_supplier_payment_entry(self.payment_entry)
            self.assertIn("simulated db failure", str(ctx.exception))
        finally:
            frappe.get_all = original_get_all

    # ------------------------------------------------------------------
    # Test 5: Fully reconciled existing payout events -> idempotent skip
    # ------------------------------------------------------------------
    def test_idempotent_skip_fully_reconciled(self):
        from sales_commission_management.sales_commission_management.payout.supplier_payment import (
            process_supplier_payment_entry,
        )

        created1 = process_supplier_payment_entry(self.payment_entry)
        self.assertTrue(created1)

        # Second call should skip (idempotent) and return empty list.
        created2 = process_supplier_payment_entry(self.payment_entry)
        self.assertEqual(created2, [])

    # ------------------------------------------------------------------
    # Test 6: Partial / inconsistent existing payout history -> transaction fails
    # ------------------------------------------------------------------
    def test_partial_existing_payout_history_fails(self):
        from sales_commission_management.sales_commission_management.payout.supplier_payment import (
            process_supplier_payment_entry,
        )

        # First process to create events.
        process_supplier_payment_entry(self.payment_entry)

        # Tamper an event amount to make history inconsistent.
        ev_name = frappe.db.get_value(
            "Commission Payout Event",
            {"source_name": self.payment_entry, "source_row": self.source_row, "docstatus": 1},
            "name",
        )
        self.assertIsNotNone(ev_name)
        frappe.db.set_value("Commission Payout Event", ev_name, "source_amount", 999, update_modified=False)

        with self.assertRaises(frappe.ValidationError) as ctx:
            process_supplier_payment_entry(self.payment_entry)
        self.assertIn("do not fully reconcile", str(ctx.exception))

    # ------------------------------------------------------------------
    # Test 7: Non-Payout event sharing same source identity -> excluded
    # ------------------------------------------------------------------
    def test_non_payout_event_excluded_from_aggregate(self):
        from sales_commission_management.sales_commission_management.payout.supplier_payment import (
            process_supplier_payment_entry,
        )
        from sales_commission_management.sales_commission_management.payout.lifecycle import (
            create_and_submit_payout_event,
        )

        # Create and submit a non-Payout (Manual Adjustment) event sharing the
        # same source identity, so it must be excluded from the Payout
        # aggregate.
        extra_key = f"nonpayout-{self.uid}"
        extra_data = {
            "event_key": extra_key,
            "event_type": "Manual Adjustment",
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
            "source_amount": 123,
            "settlement_currency": "USD",
            "settlement_amount": 123,
            "commission_currency": "USD",
            "commission_amount": 123,
            "company_currency": "USD",
            "company_amount": 123,
        }
        manual_event = create_and_submit_payout_event(extra_data)
        self.assertTrue(manual_event)
        manual_doc = frappe.get_doc("Commission Payout Event", manual_event)
        self.assertEqual(manual_doc.docstatus, 1)

        created = process_supplier_payment_entry(self.payment_entry)
        self.assertTrue(created)

    # ------------------------------------------------------------------
    # Concurrency: two Payment Entries same settlement -> no overpayment
    # ------------------------------------------------------------------
    def test_concurrent_supplier_payments_no_overpayment_mariadb(self):
        if frappe.db.db_type != "mariadb":
            self.skipTest("MariaDB locking reads required for concurrency test")

        import threading

        import sales_commission_management.sales_commission_management.payout.supplier_payment as sp_mod
        from sales_commission_management.sales_commission_management.payout.supplier_payment import (
            process_supplier_payment_entry,
        )

        # Create a second payment entry paying the same settlement.
        pe2_name = f"PE2-{self.uid}"
        if not frappe.db.exists("Payment Entry", pe2_name):
            pe2 = frappe.get_doc({
                "doctype": "Payment Entry",
                "name": pe2_name,
                "payment_type": "Pay",
                "party_type": "Supplier",
                "party": self.supplier,
                "company": self.company,
                "posting_date": frappe.utils.nowdate(),
                "paid_amount": 1000,
                "received_amount": 1000,
                "paid_from": self.bank_account,
                "paid_to": self.payable_account,
                "target_exchange_rate": 1,
                "references": [{
                    "reference_doctype": "Purchase Invoice",
                    "reference_name": self.purchase_invoice,
                    "allocated_amount": 1000,
                }],
            })
            pe2.insert(ignore_permissions=True)
            pe2.submit()
        self.payment_entry_2 = pe2_name

        frappe.db.commit()

        site = frappe.local.site
        primary = frappe.local.db

        results = [None, None]
        errors = [None, None]
        # Synchronize both workers immediately before the settlement lock is
        # acquired, so both establish ordinary-read snapshots first.
        ready_barrier = threading.Barrier(2)
        lock_barrier = threading.Barrier(2)
        ready_event = threading.Event()
        start_event = threading.Event()

        def worker(idx, pe_name):
            try:
                frappe.init(site=site)
                frappe.connect()
                frappe.set_user("Administrator")
                # Patch lock acquisition to wait on the shared barrier before
                # delegating, so both workers race for the real settlement lock.
                original_lock = sp_mod.lock_commission_settlements

                def locking_with_barrier(settlement_names):
                    lock_barrier.wait(timeout=30)
                    return original_lock(settlement_names)

                sp_mod.lock_commission_settlements = locking_with_barrier

                ready_barrier.wait(timeout=30)
                ready_event.set()
                start_event.wait(timeout=30)
                created = process_supplier_payment_entry(pe_name)
                frappe.db.commit()
                results[idx] = created
            except Exception as exc:
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

        t0 = threading.Thread(target=worker, args=(0, self.payment_entry), daemon=True)
        t1 = threading.Thread(target=worker, args=(1, self.payment_entry_2), daemon=True)
        t0.start()
        t1.start()
        try:
            if not ready_event.wait(timeout=30):
                raise RuntimeError("Worker threads did not become ready")
            # Ensure both workers cleared the ready barrier.
            start_event.set()
            t0.join(timeout=60)
            t1.join(timeout=60)
            self.assertFalse(t0.is_alive(), "worker 0 did not terminate")
            self.assertFalse(t1.is_alive(), "worker 1 did not terminate")

            frappe.local.db = primary

            # Exactly one worker must succeed; the other must fail.
            successes = [r for r in results if r]
            failures = [e for e in errors if e is not None]
            self.assertEqual(len(successes), 1, "Expected exactly one successful payout.")
            self.assertEqual(len(failures), 1, "Expected exactly one concurrency failure.")

            # The failure must be the expected financial validation error, not
            # an unrelated/connection failure.
            failure = failures[0]
            failure_msg = str(failure)
            self.assertTrue(
                ("No remaining item balances" in failure_msg)
                or ("overpaid" in failure_msg.lower())
                or ("does not fully reconcile" in failure_msg),
                "Unexpected failure type: {0}".format(failure_msg),
            )

            # Query the submitted payout ledger: net total must not exceed the
            # settlement balance.
            ledger_rows = frappe.db.sql(
                "SELECT COALESCE(SUM(settlement_amount), 0) "
                "FROM `tabCommission Payout Event` "
                "WHERE settlement = %s AND docstatus = 1 AND event_type = 'Payout'",
                (self.settlement,),
                as_list=True,
            )
            net_total = float(ledger_rows[0][0]) if ledger_rows else 0.0
            self.assertLessEqual(
                net_total, 1000.0 + 1e-9,
                "Payout ledger net total exceeds settlement balance.",
            )

        finally:
            start_event.set()
            t0.join(timeout=30)
            t1.join(timeout=30)
            frappe.local.db = primary
            try:
                frappe.db.sql("DELETE FROM `tabCommission Payout Event` WHERE source_name IN (%s, %s)", (self.payment_entry, self.payment_entry_2))
                frappe.db.commit()
            except Exception as _ce:
                print("CLEANUP ERROR:", _ce)
                try:
                    frappe.db.rollback()
                except Exception:
                    pass
            try:
                self._cleanup_uid_records(commit=True)
            except Exception as _ce:
                print("CLEANUP ERROR:", _ce)
                try:
                    frappe.db.rollback()
                except Exception:
                    pass
