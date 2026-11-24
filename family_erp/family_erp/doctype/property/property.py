import frappe
from frappe import _
from frappe.model.document import Document


class Property(Document):
	def before_save(self):
		self.ensure_cost_center()
		self.sync_eb_consumer_no()

	def sync_eb_consumer_no(self):
		if self.get("eb_services"):
			active_nos = [r.service_no for r in self.eb_services if r.service_no and r.is_active]
			if active_nos:
				self.eb_consumer_no = ", ".join(active_nos)

	def ensure_cost_center(self):
		if self.cost_center or not self.owner_company:
			return

		abbr = frappe.get_cached_value("Company", self.owner_company, "abbr")
		root_cost_center = frappe.db.get_value(
			"Cost Center",
			{"company": self.owner_company, "is_group": 1, "parent_cost_center": ("in", ("", None))},
			"name",
		)
		if not root_cost_center:
			root_cost_center = frappe.db.get_value(
				"Cost Center",
				{"company": self.owner_company, "is_group": 1},
				"name",
			)

		cc_name = f"{self.property_name} - {abbr}"
		if not frappe.db.exists("Cost Center", cc_name):
			cc = frappe.new_doc("Cost Center")
			cc.cost_center_name = self.property_name
			cc.company = self.owner_company
			cc.parent_cost_center = root_cost_center
			cc.is_group = 0
			cc.flags.ignore_permissions = True
			cc.insert()
			self.cost_center = cc.name
		else:
			self.cost_center = cc_name
