# Copyright (c) 2026, Mohammed Faisal and contributors
# For license information, please see license.txt

from frappe.model.document import Document
from family_erp.utils.rules_engine import clear_rules_cache


class FamilyAutomationRule(Document):
	def on_update(self):
		clear_rules_cache()

	def on_trash(self):
		clear_rules_cache()
