import frappe
from frappe import _
from frappe.utils import flt, fmt_money
from family_erp.family_erp.report.family_dues_statement import family_dues_statement

no_cache = 1


def get_home_page(user):
	"""
	Returns '/family' for non-admin users who have a Company User Permission.
	Called via hook 'get_website_user_home_page'.
	"""
	if not user or user in ("Guest", "Administrator"):
		return None

	roles = frappe.get_roles(user)
	if "System Manager" in roles:
		return None

	has_company_perm = frappe.db.get_value(
		"User Permission",
		{"user": user, "allow": "Company"},
		"name",
	)
	if has_company_perm:
		return "/family"
	return None


def get_context(context):
	if frappe.session.user == "Guest":
		frappe.local.flags.redirect_location = "/login?redirect-to=/family"
		raise frappe.Redirect

	user = frappe.session.user
	user_roles = frappe.get_roles(user)
	is_system_manager = "System Manager" in user_roles

	# Fetch mapped companies from Family Accounting Settings
	settings = frappe.get_single("Family Accounting Settings")
	all_mapped_companies = [m.family_company for m in settings.company_mappings if m.family_company]

	# Check User Permissions for Company
	user_allowed_companies = frappe.get_all(
		"User Permission",
		filters={"user": user, "allow": "Company"},
		pluck="for_value",
	)

	company = None
	is_admin_preview = False

	if is_system_manager and not user_allowed_companies:
		is_admin_preview = True
		req_company = frappe.form_dict.get("company")
		if req_company and req_company in all_mapped_companies:
			company = req_company
		elif all_mapped_companies:
			company = all_mapped_companies[0]
		else:
			company = "Farida Begum"
	else:
		# Family user: strictly locked to user permission company.
		# Ignore any ?company= query param to prevent IDOR!
		if user_allowed_companies:
			company = user_allowed_companies[0]
		else:
			context["no_company_linked"] = True
			context["title"] = _("Family Portal")
			return

	context["is_admin_preview"] = is_admin_preview
	context["all_companies"] = all_mapped_companies
	context["selected_company"] = company
	context["user_name"] = frappe.utils.get_fullname(user) or user
	context["title"] = f"{company} - Family Portal"

	# Date filters
	from_date = frappe.form_dict.get("from_date") or "2022-04-01"
	to_date = frappe.form_dict.get("to_date") or "2027-12-31"
	context["from_date"] = from_date
	context["to_date"] = to_date

	# Fetch statement data
	filters = {
		"company": company,
		"from_date": from_date,
		"to_date": to_date,
	}

	try:
		columns, raw_data = family_dues_statement.execute(filters)
	except Exception as e:
		raw_data = []
		context["statement_error"] = str(e)

	# Format rows and compute balance summary
	statement_rows = []
	net_balance = 0.0
	total_debit = 0.0
	total_credit = 0.0

	for row in raw_data:
		if row.get("description") == "Total":
			total_debit = flt(row.get("debit"))
			total_credit = flt(row.get("credit"))
			net_balance = flt(row.get("balance"))
			continue

		row_debit = flt(row.get("debit"))
		row_credit = flt(row.get("credit"))
		row_bal = flt(row.get("balance"))

		statement_rows.append({
			"posting_date": row.get("posting_date"),
			"voucher_type": row.get("voucher_type"),
			"voucher_no": row.get("voucher_no"),
			"description": row.get("description"),
			"debit": row_debit,
			"credit": row_credit,
			"balance": row_bal,
			"debit_formatted": fmt_inr(row_debit) if row_debit else "-",
			"credit_formatted": fmt_inr(row_credit) if row_credit else "-",
			"balance_formatted": fmt_inr(abs(row_bal)),
			"balance_raw": row_bal,
		})

	context["statement_rows"] = statement_rows
	context["total_debit_formatted"] = fmt_inr(total_debit)
	context["total_credit_formatted"] = fmt_inr(total_credit)
	context["net_balance_raw"] = net_balance
	context["net_balance_formatted"] = fmt_inr(abs(net_balance))

	# Plain-English balance status based on family_dues_statement convention:
	# running_balance = credit - debit (clearing account is liability 'Due to Faisal Household')
	# positive: Family member owes Faisal
	# negative: Credit with Faisal (Faisal owes family member)
	# zero: Settled
	if net_balance > 0.005:
		context["balance_status_text"] = "Net Balance Payable to Faisal"
		context["balance_status_badge"] = "payable"
		context["balance_subtext"] = f"As of {to_date}, you have a balance due to Faisal Household."
	elif net_balance < -0.005:
		context["balance_status_text"] = "Credit with Faisal"
		context["balance_status_badge"] = "credit"
		context["balance_subtext"] = f"As of {to_date}, you have an advance credit with Faisal Household."
	else:
		context["balance_status_text"] = "All Dues Settled"
		context["balance_status_badge"] = "settled"
		context["balance_subtext"] = f"All accounts are fully settled as of {to_date}."

	# Fetch Properties for this company
	properties = frappe.get_all(
		"Property",
		filters={"owner_company": company},
		fields=[
			"name",
			"property_name",
			"property_type",
			"property_tax_assessment_no",
			"water_tax_assessment_no",
			"water_consumer_no",
			"eb_consumer_no",
			"address",
			"cost_center",
		],
		order_by="property_name asc",
	)

	total_active_meters = 0
	for prop in properties:
		services = frappe.get_all(
			"Property EB Service",
			filters={"parent": prop["name"], "is_active": 1},
			fields=["service_no", "consumer_name", "meter_label"],
			order_by="idx asc, creation asc",
		)
		prop["eb_services"] = services
		total_active_meters += len(services)

	context["properties"] = properties
	context["properties_count"] = len(properties)
	context["active_meters_count"] = total_active_meters


def fmt_inr(amount):
	"""
	Formats a number in Indian Rupee format (e.g., ₹ 3,33,513.49).
	"""
	val = flt(amount)
	return fmt_money(val, precision=2, currency="INR")
