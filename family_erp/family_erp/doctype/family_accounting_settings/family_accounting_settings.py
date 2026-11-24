import frappe
from frappe import _
from frappe.model.document import Document


class FamilyAccountingSettings(Document):
	def get_mapping(self, family_company):
		"""Return matching Family Company Mapping row for family_company or throw ValidationError."""
		if not family_company:
			frappe.throw(_("Family Company is required to fetch accounting mapping."))

		for mapping in self.company_mappings:
			if mapping.family_company == family_company:
				return mapping

		frappe.throw(
			_("No Family Company Mapping found in Family Accounting Settings for company '{0}'.").format(
				family_company
			),
			title=_("Mapping Missing"),
		)
