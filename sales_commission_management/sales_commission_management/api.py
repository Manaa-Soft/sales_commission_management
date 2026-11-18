import frappe
from frappe.utils import flt


def _enforce_sales_person_scope(sales_person):
    """Non-privileged users may only query their own linked Sales Person."""
    privileged = {"System Manager", "Accounts Manager", "Sales Manager"}
    user_roles = set(frappe.get_roles(frappe.session.user))
    if privileged & user_roles:
        return sales_person
    # resolve linked sales person for the current user
    linked = frappe.db.get_value("Sales Person", {"user": frappe.session.user}, "name")
    if linked:
        return linked
    # No linked Sales Person for this user: deny (return None -> empty result set)
    # rather than trusting a caller-supplied sales_person.
    return None


@frappe.whitelist(methods=["GET"])
def get_commission_summary(
    sales_person: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    status: str | None = None,
    company: str | None = None,
):
    sales_person = _enforce_sales_person_scope(sales_person)
    filters = {}
    if sales_person:
        filters["sales_person"] = sales_person
    if company:
        filters["company"] = company
    if status:
        filters["status"] = status
    if from_date and to_date:
        filters["posting_date"] = ["between", [from_date, to_date]]
    elif from_date:
        filters["posting_date"] = [">=", from_date]
    elif to_date:
        filters["posting_date"] = ["<=", to_date]

    entries = frappe.get_list(
        "Commission Entry",
        filters=filters,
        fields=[
            "name",
            "sales_invoice",
            "posting_date",
            "company",
            "beneficiary_type",
            "sales_person",
            "sales_partner",
            "item_code",
            "commission_basis",
            "commission_basis_amount",
            "earned_commission",
            "payable_amount",
            "paid_amount",
            "outstanding_amount",
            "status",
            "transaction_currency",
            "company_currency",
            "exchange_rate",
        ],
        order_by="posting_date desc",
    )
    return entries


@frappe.whitelist(methods=["GET"])
def get_commission_totals(
    sales_person: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    company: str | None = None,
):
    sales_person = _enforce_sales_person_scope(sales_person)
    filters = {}
    if sales_person:
        filters["sales_person"] = sales_person
    if company:
        filters["company"] = company
    if from_date and to_date:
        filters["posting_date"] = ["between", [from_date, to_date]]

    rows = frappe.get_list(
        "Commission Entry",
        filters=filters,
        fields=["earned_commission", "paid_amount", "outstanding_amount", "status"],
    )
    totals = {
        "earned": 0.0,
        "paid": 0.0,
        "outstanding": 0.0,
        "count": len(rows),
    }
    for r in rows:
        totals["earned"] += flt(r.earned_commission)
        totals["paid"] += flt(r.paid_amount)
        totals["outstanding"] += flt(r.outstanding_amount)
    return totals
