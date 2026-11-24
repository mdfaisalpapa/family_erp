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
	context["csrf_token"] = frappe.sessions.get_csrf_token()
	context["title"] = f"{company} - Family Portal"

	# Date filters
	from_date = frappe.form_dict.get("from_date") or "2022-04-01"
	to_date = frappe.form_dict.get("to_date") or "2027-12-31"
	context["from_date"] = from_date
	context["to_date"] = to_date

	# Fetch statement data (clearing-account dues mode, or GL fallback for parent company)
	filters = {
		"company": company,
		"from_date": from_date,
		"to_date": to_date,
	}

	context["is_gl_mode"] = False
	context["gl_accounts"] = []
	context["selected_account"] = "ALL_BANK_CASH"
	context["gl_truncated_note"] = ""

	raw_data = []
	use_gl_mode = False
	try:
		columns, raw_data = family_dues_statement.execute(filters)
	except Exception as e:
		err_msg = str(e)
		if "No clearing account found" in err_msg:
			use_gl_mode = True
		else:
			context["statement_error"] = err_msg

	if use_gl_mode:
		_populate_gl_statement(context, company, from_date, to_date)
	else:
		_populate_dues_statement(context, raw_data, to_date)

	# Financial statements (for all companies)
	_populate_balance_sheet(context, company, to_date)
	_populate_income_expenditure(context, company, from_date, to_date)

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


def _populate_dues_statement(context, raw_data, to_date):
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


def _populate_gl_statement(context, company, from_date, to_date):
	context["is_gl_mode"] = True
	context.pop("statement_error", None)

	gl_accounts = frappe.db.sql(
		"""
		SELECT DISTINCT
			acc.name,
			acc.account_name,
			acc.account_type,
			acc.root_type
		FROM `tabAccount` acc
		INNER JOIN `tabGL Entry` gle ON gle.account = acc.name
		WHERE acc.company = %(company)s
			AND acc.is_group = 0
			AND gle.company = %(company)s
			AND gle.is_cancelled = 0
		ORDER BY acc.root_type ASC, acc.account_name ASC
		""",
		{"company": company},
		as_dict=True,
	)
	context["gl_accounts"] = gl_accounts

	account_map = {a["name"]: a for a in gl_accounts}
	selected_account = frappe.form_dict.get("account") or "ALL_BANK_CASH"
	if selected_account not in ("ALL_BANK_CASH", "ALL_ACCOUNTS") and selected_account not in account_map:
		selected_account = "ALL_BANK_CASH"
	context["selected_account"] = selected_account

	base_conds = ["gle.company = %(company)s", "gle.is_cancelled = 0"]
	params = {
		"company": company,
		"from_date": from_date,
		"to_date": to_date,
	}

	if selected_account == "ALL_BANK_CASH":
		bank_cash_names = [a["name"] for a in gl_accounts if a.get("account_type") in ("Bank", "Cash")]
		if not bank_cash_names:
			_set_empty_gl_context(context, "Bank & Cash Balance", to_date)
			return
		base_conds.append("gle.account IN %(accounts)s")
		params["accounts"] = tuple(bank_cash_names)
		scope_label = "Bank & Cash Balance"
	elif selected_account == "ALL_ACCOUNTS":
		scope_label = "All Accounts Net Ledger Balance"
	else:
		base_conds.append("gle.account = %(selected_account)s")
		params["selected_account"] = selected_account
		scope_label = account_map[selected_account]["account_name"]

	base_where = " AND ".join(base_conds)

	# Opening balance prior to from_date (standard GL convention: debit - credit)
	opening_res = frappe.db.sql(
		f"""
		SELECT COALESCE(SUM(gle.debit - gle.credit), 0)
		FROM `tabGL Entry` gle
		WHERE {base_where}
			AND gle.posting_date < %(from_date)s
		""",
		params,
	)
	opening_balance = flt(opening_res[0][0]) if opening_res else 0.0

	# Period totals & row count across entire [from_date, to_date] window
	period_agg = frappe.db.sql(
		f"""
		SELECT
			COUNT(*) AS cnt,
			COALESCE(SUM(gle.debit), 0) AS total_debit,
			COALESCE(SUM(gle.credit), 0) AS total_credit
		FROM `tabGL Entry` gle
		WHERE {base_where}
			AND gle.posting_date >= %(from_date)s
			AND gle.posting_date <= %(to_date)s
		""",
		params,
		as_dict=True,
	)
	total_count = int(period_agg[0]["cnt"]) if period_agg else 0
	total_debit = flt(period_agg[0]["total_debit"]) if period_agg else 0.0
	total_credit = flt(period_agg[0]["total_credit"]) if period_agg else 0.0
	closing_balance = opening_balance + total_debit - total_credit

	# Fetch period rows (show all if <= 1000, otherwise latest 500)
	if total_count > 1000:
		entries_desc = frappe.db.sql(
			f"""
			SELECT
				gle.posting_date,
				gle.voucher_type,
				gle.voucher_no,
				gle.account,
				acc.account_name,
				gle.debit,
				gle.credit,
				gle.remarks,
				je.user_remark
			FROM `tabGL Entry` gle
			LEFT JOIN `tabAccount` acc ON gle.account = acc.name
			LEFT JOIN `tabJournal Entry` je ON gle.voucher_no = je.name AND gle.voucher_type = 'Journal Entry'
			WHERE {base_where}
				AND gle.posting_date >= %(from_date)s
				AND gle.posting_date <= %(to_date)s
			ORDER BY gle.posting_date DESC, gle.creation DESC
			LIMIT 500
			""",
			params,
			as_dict=True,
		)
		entries = list(reversed(entries_desc))
		shown_net = sum(flt(e.debit) - flt(e.credit) for e in entries)
		start_balance = closing_balance - shown_net
		opening_desc = f"Brought Forward Balance (showing latest 500 of {total_count} entries)"
		context["gl_truncated_note"] = f"Showing latest 500 of {total_count} ledger entries in this period. Period totals & closing balance reflect all {total_count} entries."
	else:
		entries = frappe.db.sql(
			f"""
			SELECT
				gle.posting_date,
				gle.voucher_type,
				gle.voucher_no,
				gle.account,
				acc.account_name,
				gle.debit,
				gle.credit,
				gle.remarks,
				je.user_remark
			FROM `tabGL Entry` gle
			LEFT JOIN `tabAccount` acc ON gle.account = acc.name
			LEFT JOIN `tabJournal Entry` je ON gle.voucher_no = je.name AND gle.voucher_type = 'Journal Entry'
			WHERE {base_where}
				AND gle.posting_date >= %(from_date)s
				AND gle.posting_date <= %(to_date)s
			ORDER BY gle.posting_date ASC, gle.creation ASC
			""",
			params,
			as_dict=True,
		)
		start_balance = opening_balance
		opening_desc = "Opening Balance"

	statement_rows = []
	running_balance = start_balance

	if abs(start_balance) >= 0.005 or total_count > 1000:
		statement_rows.append({
			"posting_date": from_date,
			"voucher_type": "",
			"voucher_no": "",
			"description": opening_desc,
			"debit": 0.0,
			"credit": 0.0,
			"balance": running_balance,
			"debit_formatted": "-",
			"credit_formatted": "-",
			"balance_formatted": fmt_inr(abs(running_balance)),
			"balance_raw": running_balance,
		})

	show_account_prefix = selected_account in ("ALL_BANK_CASH", "ALL_ACCOUNTS")
	for entry in entries:
		dr = flt(entry.debit)
		cr = flt(entry.credit)
		running_balance += dr - cr
		raw_desc = entry.user_remark or entry.remarks or entry.voucher_type or ""
		if show_account_prefix and entry.get("account_name"):
			desc = f"[{entry.account_name}] {raw_desc}".strip()
		else:
			desc = raw_desc

		statement_rows.append({
			"posting_date": entry.posting_date,
			"voucher_type": entry.voucher_type,
			"voucher_no": entry.voucher_no,
			"description": desc,
			"debit": dr,
			"credit": cr,
			"balance": running_balance,
			"debit_formatted": fmt_inr(dr) if dr else "-",
			"credit_formatted": fmt_inr(cr) if cr else "-",
			"balance_formatted": fmt_inr(abs(running_balance)),
			"balance_raw": running_balance,
		})

	context["statement_rows"] = statement_rows
	context["total_debit_formatted"] = fmt_inr(total_debit)
	context["total_credit_formatted"] = fmt_inr(total_credit)
	context["net_balance_raw"] = closing_balance
	context["net_balance_formatted"] = fmt_inr(abs(closing_balance))
	context["balance_status_text"] = scope_label

	if closing_balance > 0.005:
		context["balance_status_badge"] = "credit"
		context["balance_subtext"] = f"{scope_label} as of {to_date} (Debit / Net Positive Balance)."
	elif closing_balance < -0.005:
		context["balance_status_badge"] = "payable"
		context["balance_subtext"] = f"{scope_label} as of {to_date} (Credit / Liability Balance)."
	else:
		context["balance_status_badge"] = "settled"
		context["balance_subtext"] = f"{scope_label} is settled (zero balance) as of {to_date}."


def _set_empty_gl_context(context, scope_label, to_date):
	context["statement_rows"] = []
	context["total_debit_formatted"] = fmt_inr(0)
	context["total_credit_formatted"] = fmt_inr(0)
	context["net_balance_raw"] = 0.0
	context["net_balance_formatted"] = fmt_inr(0)
	context["balance_status_text"] = scope_label
	context["balance_status_badge"] = "settled"
	context["balance_subtext"] = f"No transactions recorded for {scope_label} as of {to_date}."


def _populate_balance_sheet(context, company, to_date):
	rows = frappe.db.sql(
		"""
		SELECT
			acc.name AS account,
			acc.account_name,
			acc.parent_account,
			acc.root_type,
			SUM(gle.debit - gle.credit) AS raw_balance
		FROM `tabGL Entry` gle
		INNER JOIN `tabAccount` acc ON gle.account = acc.name
		WHERE gle.company = %(company)s
			AND gle.is_cancelled = 0
			AND gle.posting_date <= %(to_date)s
			AND acc.root_type IN ('Asset', 'Liability', 'Equity')
		GROUP BY acc.name, acc.account_name, acc.parent_account, acc.root_type
		ORDER BY acc.root_type ASC, acc.parent_account ASC, acc.account_name ASC
		""",
		{"company": company, "to_date": to_date},
		as_dict=True,
	)

	bs_assets = []
	bs_liabilities = []
	bs_equity = []

	for row in rows:
		raw_bal = flt(row.raw_balance)
		if row.root_type == "Asset":
			bal = raw_bal
		else:
			bal = -raw_bal

		if abs(bal) < 0.01:
			continue

		item = {
			"account": row.account,
			"account_name": row.account_name,
			"parent_account": row.parent_account or "",
			"balance": bal,
			"balance_formatted": fmt_inr(bal),
		}
		if row.root_type == "Asset":
			bs_assets.append(item)
		elif row.root_type == "Liability":
			bs_liabilities.append(item)
		else:
			bs_equity.append(item)

	total_assets = sum(r["balance"] for r in bs_assets)
	total_liabilities = sum(r["balance"] for r in bs_liabilities)
	total_equity = sum(r["balance"] for r in bs_equity)
	net_worth = total_assets - total_liabilities

	context["bs_assets"] = bs_assets
	context["bs_liabilities"] = bs_liabilities
	context["bs_equity"] = bs_equity
	context["bs_total_assets"] = total_assets
	context["bs_total_liabilities"] = total_liabilities
	context["bs_total_equity"] = total_equity
	context["bs_net_worth"] = net_worth
	context["bs_total_assets_formatted"] = fmt_inr(total_assets)
	context["bs_total_liabilities_formatted"] = fmt_inr(total_liabilities)
	context["bs_total_equity_formatted"] = fmt_inr(total_equity)
	context["bs_net_worth_formatted"] = fmt_inr(net_worth)


def _populate_income_expenditure(context, company, from_date, to_date):
	rows = frappe.db.sql(
		"""
		SELECT
			acc.name AS account,
			acc.account_name,
			acc.parent_account,
			acc.root_type,
			SUM(gle.credit - gle.debit) AS raw_net_credit
		FROM `tabGL Entry` gle
		INNER JOIN `tabAccount` acc ON gle.account = acc.name
		WHERE gle.company = %(company)s
			AND gle.is_cancelled = 0
			AND gle.posting_date BETWEEN %(from_date)s AND %(to_date)s
			AND acc.root_type IN ('Income', 'Expense')
		GROUP BY acc.name, acc.account_name, acc.parent_account, acc.root_type
		ORDER BY acc.root_type ASC, acc.parent_account ASC, acc.account_name ASC
		""",
		{"company": company, "from_date": from_date, "to_date": to_date},
		as_dict=True,
	)

	ie_income = []
	ie_expense = []

	for row in rows:
		raw_cr = flt(row.raw_net_credit)
		if row.root_type == "Income":
			amt = raw_cr
		else:
			amt = -raw_cr

		if abs(amt) < 0.01:
			continue

		item = {
			"account": row.account,
			"account_name": row.account_name,
			"parent_account": row.parent_account or "",
			"amount": amt,
			"amount_formatted": fmt_inr(amt),
		}
		if row.root_type == "Income":
			ie_income.append(item)
		else:
			ie_expense.append(item)

	total_income = sum(r["amount"] for r in ie_income)
	total_expense = sum(r["amount"] for r in ie_expense)
	net_surplus = total_income - total_expense

	context["ie_income"] = ie_income
	context["ie_expense"] = ie_expense
	context["ie_total_income"] = total_income
	context["ie_total_expense"] = total_expense
	context["ie_net_surplus"] = net_surplus
	context["ie_total_income_formatted"] = fmt_inr(total_income)
	context["ie_total_expense_formatted"] = fmt_inr(total_expense)
	context["ie_net_surplus_formatted"] = fmt_inr(net_surplus)


def fmt_inr(amount):
	"""
	Formats a number in Indian Rupee format (e.g., ₹ 3,33,513.49).
	"""
	val = flt(amount)
	return fmt_money(val, precision=2, currency="INR")
