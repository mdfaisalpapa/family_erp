# Copyright (c) 2026, Mohammed Faisal and contributors
# For license information, please see license.txt

import frappe
from frappe import _


FAMILY_VIEWER_ROLE = "Family Viewer"
FAMILY_PORTAL_USER_ROLE = "Family Portal User"
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


def _configure_read_role(role_name, allow_desk=False):
	"""
	Creates or updates a portal role with strict Read + Report permissions
	across RESTRICTED_DOCTYPES. Keeps desk_access = 0 by default so Website Users can hold it.
	"""
	if not frappe.db.exists("Role", role_name):
		role = frappe.new_doc("Role")
		role.role_name = role_name
		role.desk_access = 1 if allow_desk else 0
		role.insert(ignore_permissions=True)
		print(f"Created Role: {role_name}")
	else:
		role = frappe.get_doc("Role", role_name)
		role.desk_access = 1 if allow_desk else 0
		role.flags.ignore_permissions = True
		role.save()

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

	for dt in RESTRICTED_DOCTYPES:
		if not frappe.db.exists("DocType", dt):
			continue

		existing_perm = frappe.db.get_value(
			"Custom DocPerm",
			{"parent": dt, "role": role_name, "permlevel": 0},
			"name",
		)
		if not existing_perm:
			existing_standard_perm = frappe.db.get_value(
				"DocPerm",
				{"parent": dt, "role": role_name, "permlevel": 0},
				"name",
			)
			if not existing_standard_perm:
				frappe.permissions.add_permission(dt, role_name, 0)

		for ptype, val in props.items():
			try:
				frappe.permissions.update_permission_property(
					dt, role_name, 0, ptype, val, validate=False
				)
			except Exception:
				pass

	frappe.db.commit()
	print(f"Configured strict Read + Report permissions for '{role_name}' (desk_access={1 if allow_desk else 0})")


def setup_family_viewer_role(allow_desk=False):
	"""Creates or updates Role 'Family Viewer' (read-only portal viewer)."""
	_configure_read_role(FAMILY_VIEWER_ROLE, allow_desk=allow_desk)


def setup_family_portal_user_role(allow_desk=False):
	"""Creates or updates Role 'Family Portal User' (portal read + API write, desk_access=0)."""
	_configure_read_role(FAMILY_PORTAL_USER_ROLE, allow_desk=allow_desk)


def provision_family_user(email, first_name, company, password=None, allow_desk=False, role=None):
	"""
	Creates or updates a user for portal access.
	If allow_desk is False (default): sets user_type = 'Website User' for portal-only access.
	If allow_desk is True: sets user_type = 'System User'.
	Creates User Permission locked to company with apply_to_all_doctypes=1.
	Sets password if provided.
	"""
	target_role = role or FAMILY_VIEWER_ROLE
	_configure_read_role(target_role, allow_desk=allow_desk)

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

	# Set portal role
	user.roles = []
	user.append("roles", {"role": target_role})
	user.default_workspace = "Family ERP" if allow_desk else ""

	user.flags.ignore_permissions = True
	user.flags.ignore_password_policy = True
	user.save()

	if password:
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
	print(f"Provisioned user {email} (Role: {target_role}, Desk: {allow_desk}) locked to company {company}")
	return user
