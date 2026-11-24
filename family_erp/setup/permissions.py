# Copyright (c) 2026, Mohammed Faisal and contributors
# For license information, please see license.txt

import frappe
from frappe import _


FAMILY_VIEWER_ROLE = "Family Viewer"
RESTRICTED_DOCTYPES = [
	"Company",
	"Account",
	"Journal Entry",
	"GL Entry",
	"Sales Invoice",
	"Payment Entry",
	"Property",
	"Lease Agreement",
	"Tenant Monthly Bill",
]


def setup_family_viewer_role(allow_desk=False):
	"""
	Creates or updates Role 'Family Viewer'.
	If allow_desk is False, sets desk_access = 0 so Website Users can hold it.
	Grants strictly Read + Report (read=1, report=1, write=0, create=0, delete=0, submit=0, cancel=0)
	on key financial and property DocTypes.
	"""
	if not frappe.db.exists("Role", FAMILY_VIEWER_ROLE):
		role = frappe.new_doc("Role")
		role.role_name = FAMILY_VIEWER_ROLE
		role.desk_access = 1 if allow_desk else 0
		role.insert(ignore_permissions=True)
		print(f"Created Role: {FAMILY_VIEWER_ROLE}")
	else:
		role = frappe.get_doc("Role", FAMILY_VIEWER_ROLE)
		role.desk_access = 1 if allow_desk else 0
		role.flags.ignore_permissions = True
		role.save()

	for dt in RESTRICTED_DOCTYPES:
		if not frappe.db.exists("DocType", dt):
			continue

		# Check if permission already exists for this role at permlevel 0
		existing_perm = frappe.db.get_value(
			"Custom DocPerm",
			{"parent": dt, "role": FAMILY_VIEWER_ROLE, "permlevel": 0},
			"name",
		)
		if not existing_perm:
			# Also check standard DocPerm
			existing_standard_perm = frappe.db.get_value(
				"DocPerm",
				{"parent": dt, "role": FAMILY_VIEWER_ROLE, "permlevel": 0},
				"name",
			)
			if not existing_standard_perm:
				frappe.permissions.add_permission(dt, FAMILY_VIEWER_ROLE, 0)

		# Explicitly set permissions: read=1, report=1, write=0, create=0, delete=0, submit=0, cancel=0
		props = {
			"read": 1,
			"report": 1,
			"write": 0,
			"create": 0,
			"delete": 0,
			"submit": 0,
			"cancel": 0,
			"amend": 0,
			"print": 1,
			"export": 1,
		}
		for ptype, val in props.items():
			try:
				frappe.permissions.update_permission_property(
					dt, FAMILY_VIEWER_ROLE, 0, ptype, val, validate=False
				)
			except Exception:
				pass

	frappe.db.commit()
	print(f"Configured strict Read + Report permissions for '{FAMILY_VIEWER_ROLE}' (desk_access={1 if allow_desk else 0})")


def provision_family_user(email, first_name, company, password=None, allow_desk=False):
	"""
	Creates or updates a user for portal access.
	If allow_desk is False (default): sets user_type = 'Website User' for portal-only access.
	If allow_desk is True: sets user_type = 'System User' with Family Viewer role.
	Creates User Permission locked to company with apply_to_all_doctypes=1.
	Sets password if provided.
	"""
	setup_family_viewer_role(allow_desk=allow_desk)

	if not frappe.db.exists("Company", company):
		frappe.throw(_("Company {0} does not exist").format(company))

	if frappe.db.exists("User", email):
		user = frappe.get_doc("User", email)
		user.first_name = first_name
	else:
		user = frappe.new_doc("User")
		user.email = email
		user.first_name = first_name
		user.send_welcome_email = 0

	user.user_type = "System User" if allow_desk else "Website User"

	# Set Family Viewer role
	user.roles = []
	user.append("roles", {"role": FAMILY_VIEWER_ROLE})
	user.default_workspace = "Family ERP" if allow_desk else ""

	user.flags.ignore_permissions = True
	user.flags.ignore_password_policy = True
	user.save()

	if password:
		from frappe.utils.password_strength import test_password_strength
		user_pass = frappe.get_doc("User", email)
		user_pass.new_password = password
		user_pass.flags.ignore_permissions = True
		user_pass.flags.ignore_password_policy = True
		user_pass.save()

	# Create User Permission for Company
	user_perm_name = frappe.db.get_value(
		"User Permission",
		{"user": email, "allow": "Company", "for_value": company},
		"name",
	)

	if not user_perm_name:
		up = frappe.new_doc("User Permission")
		up.user = email
		up.allow = "Company"
		up.for_value = company
		up.apply_to_all_doctypes = 1
		up.is_default = 1
		up.flags.ignore_permissions = True
		up.insert()
	else:
		up = frappe.get_doc("User Permission", user_perm_name)
		up.apply_to_all_doctypes = 1
		up.is_default = 1
		up.flags.ignore_permissions = True
		up.save()

	# Clean up any existing User Permissions for other companies
	other_perms = frappe.get_all(
		"User Permission",
		filters={"user": email, "allow": "Company", "for_value": ["!=", company]},
		pluck="name",
	)
	for p_name in other_perms:
		frappe.delete_doc("User Permission", p_name, ignore_permissions=True)

	frappe.db.commit()
	print(f"Provisioned user {email} (Desk: {allow_desk}) locked to company {company}")
	return user
