import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields
from family_erp.setup.permissions import setup_family_viewer_role


def after_install():
	custom_fields = {
		"Journal Entry": [
			{
				"fieldname": "firefly_journal_id",
				"label": "Firefly Journal ID",
				"fieldtype": "Data",
				"insert_after": "user_remark",
				"read_only": 1,
				"unique": 1,
			}
		]
	}
	create_custom_fields(custom_fields, update=True)
	setup_family_viewer_role()

