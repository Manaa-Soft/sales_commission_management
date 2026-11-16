import frappe
from frappe import _
from frappe.model.document import Document


class CommissionTarget(Document):
    def validate(self):
        self._validate_beneficiary()
        self._validate_metric_unit()
        self._validate_uniqueness()

    def _validate_beneficiary(self):
        bt = self.beneficiary_type
        if bt == "Sales Person":
            if not self.sales_person:
                frappe.throw(_("Sales Person is required for Sales Person target."))
            if self.sales_partner:
                frappe.throw(_("Sales Partner must be empty for Sales Person target."))
        elif bt == "Sales Partner":
            if not self.sales_partner:
                frappe.throw(_("Sales Partner is required for Sales Partner target."))
            if self.sales_person:
                frappe.throw(_("Sales Person must be empty for Sales Partner target."))
        else:
            frappe.throw(_("Invalid beneficiary type."))

    def _validate_metric_unit(self):
        metric = self.target_metric
        if metric in ("Net Sales", "Gross Profit", "Collected Net Sales"):
            if not self.target_currency:
                frappe.throw(_("Target currency is required for monetary targets."))
        elif metric == "Quantity":
            if not self.target_uom:
                frappe.throw(_("Target UOM is required for quantity targets."))

    def _validate_uniqueness(self):
        filters = {
            "beneficiary_type": self.beneficiary_type,
            "target_metric": self.target_metric,
            "period_type": self.period_type,
            "docstatus": ["!=", 2],
        }
        if self.beneficiary_type == "Sales Person":
            filters["sales_person"] = self.sales_person
        else:
            filters["sales_partner"] = self.sales_partner
        if self.item_group:
            filters["item_group"] = self.item_group
        if self.name:
            filters["name"] = ["!=", self.name]
        existing = frappe.db.exists("Commission Target", filters)
        if existing:
            frappe.throw(_("A target with the same beneficiary, metric, period, and item group already exists."))
