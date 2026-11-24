import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt

SERVICE_ITEMS = {
	"Monthly Rent": "Monthly Rent Charges",
	"Electricity Charges": "Electricity (EB) Utility Charges",
	"Water Charges": "Water & Sewerage Charges",
	"Other Property Charges": "Other Property Miscellaneous Charges",
}


def ensure_service_item(item_code, item_name):
	if not frappe.db.exists("Item", item_code):
		item = frappe.new_doc("Item")
		item.item_code = item_code
		item.item_name = item_name
		item.item_group = "Services"
		item.is_stock_item = 0
		item.include_item_in_manufacturing = 0
		item.flags.ignore_permissions = True
		item.flags.ignore_mandatory = True
		item.insert()
	return item_code


class TenantMonthlyBill(Document):
	def validate(self):
		self.calculate_charges()

	def calculate_charges(self):
		if self.eb_curr_reading is not None and self.eb_prev_reading is not None:
			diff = flt(self.eb_curr_reading) - flt(self.eb_prev_reading)
			self.eb_units = max(diff, 0.0)
		else:
			self.eb_units = 0.0

		if flt(self.eb_units) > 0 and flt(self.eb_rate) > 0:
			self.eb_amount = round(flt(self.eb_units) * flt(self.eb_rate), 2)
		elif not self.eb_amount:
			self.eb_amount = 0.0

		self.total_amount = round(
			flt(self.rent_amount)
			+ flt(self.eb_amount)
			+ flt(self.water_amount)
			+ flt(self.other_charges),
			2,
		)

	def on_submit(self):
		for code, name in SERVICE_ITEMS.items():
			ensure_service_item(code, name)

		cost_center = frappe.db.get_value("Property", self.property, "cost_center")
		si = frappe.new_doc("Sales Invoice")
		si.company = self.owner_company
		si.customer = self.tenant
		si.posting_date = self.posting_date
		si.remarks = f"{self.name}: Rent & Utilities bill for {self.billing_month} ({self.property})"

		bill_lines = [
			("Monthly Rent", flt(self.rent_amount), f"Monthly Rent for {self.billing_month}"),
			("Electricity Charges", flt(self.eb_amount), f"EB Units: {self.eb_units} @ {self.eb_rate}/unit"),
			("Water Charges", flt(self.water_amount), "Water Charges"),
			("Other Property Charges", flt(self.other_charges), self.other_charges_note or "Other Charges"),
		]

		for item_code, rate, desc in bill_lines:
			if rate > 0:
				si.append(
					"items",
					{
						"item_code": item_code,
						"qty": 1,
						"rate": rate,
						"description": desc,
						"cost_center": cost_center,
					},
				)

		si.flags.ignore_permissions = True
		si.insert()
		si.submit()

		self.db_set("sales_invoice", si.name)

	def on_cancel(self):
		self.ignore_linked_doctypes = ("Sales Invoice",)
		if self.sales_invoice and frappe.db.exists("Sales Invoice", self.sales_invoice):
			si_doc = frappe.get_doc("Sales Invoice", self.sales_invoice)
			if si_doc.docstatus == 1:
				si_doc.flags.ignore_permissions = True
				si_doc.cancel()
