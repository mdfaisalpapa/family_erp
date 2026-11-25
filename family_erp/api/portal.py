# Copyright (c) 2026, Mohammed Faisal and contributors
# For license information, please see license.txt

import json
import frappe
from frappe import _
from frappe.desk.doctype.tag.tag import DocTags
from frappe.utils import add_days, cint, flt, get_first_day, get_last_day, getdate, nowdate, today
from family_erp.utils.rules_engine import evaluate_rules, serialize_rule_doc


CREDIT_CARD_SQL = "(acc.account_name LIKE 'Credit Card%%' OR acc.parent_account LIKE '%%Credit Card%%')"


def _resolve_portal_company():
	"""
	Resolves the authenticated user's locked Company and write capability.
	- Rejects Guest with PermissionError.
	- System Manager without Company User Permission may select via ?company= (defaults to primary_company).
	- All other users are hard-locked to their Company User Permission[0].
	Returns (company, can_write).
	"""
	user = frappe.session.user
	if not user or user == "Guest":
		frappe.throw(_("Authentication required."), frappe.PermissionError)

	user_roles = set(frappe.get_roles(user))
	is_system_manager = "System Manager" in user_roles

	user_allowed_companies = frappe.get_all(
		"User Permission",
		filters={"user": user, "allow": "Company"},
		pluck="for_value",
		order_by="is_default DESC, creation ASC",
	)

	if is_system_manager and not user_allowed_companies:
		company = frappe.form_dict.get("company")
		if not company:
			settings = frappe.get_single("Family Accounting Settings")
			company = settings.primary_company
			if not company and settings.company_mappings:
				company = settings.company_mappings[0].family_company
			if not company:
				company = "Faisal Household"
	else:
		if not user_allowed_companies:
			frappe.throw(_("No Company User Permission linked to your account."), frappe.PermissionError)
		company = user_allowed_companies[0]

	if not frappe.db.exists("Company", company):
		frappe.throw(_("Company '{0}' does not exist.").format(company), frappe.ValidationError)

	can_write = ("Family Portal User" in user_roles) or is_system_manager
	return company, can_write


def _parse_csv_list(raw, split_newlines=False):
	if not raw:
		return []
	txt = str(raw).replace("\r\n", "\n")
	if split_newlines:
		txt = txt.replace("\n", ",")
	seen = []
	for part in txt.split(","):
		val = part.strip()
		if val and val not in seen:
			seen.append(val)
	return seen


def _normalize_tx_type(tx_type):
	val = (tx_type or "").strip()
	if val == "Withdrawal":
		return "Expense"
	if val == "Deposit":
		return "Income"
	return val


def _classify_account_group(acc):
	acc_type = acc.get("account_type") or ""
	acc_name = acc.get("account_name") or ""
	parent_acc = acc.get("parent_account") or ""
	root_type = acc.get("root_type") or ""

	if acc_type in ("Bank", "Cash"):
		return acc_type
	if "Credit Card" in acc_name or "Credit Card" in parent_acc:
		return "Credit Card"
	if acc_name.startswith("Due from") or acc_name.startswith("Due to"):
		return "Family Clearing"
	return root_type or "Other"


def _is_liquid_account(row):
	acc_type = row.get("account_type") or ""
	acc_name = row.get("account_name") or ""
	parent_acc = row.get("parent_account") or ""
	return acc_type in ("Bank", "Cash") or "Credit Card" in acc_name or "Credit Card" in parent_acc


def _infer_je_details(je, jea_rows, attachments_count):
	debits = [r for r in jea_rows if flt(r.debit) > 0]
	credits = [r for r in jea_rows if flt(r.credit) > 0]

	from_account = credits[0].account if credits else ""
	from_account_name = credits[0].account_name if credits else ""
	to_account = debits[0].account if debits else ""
	to_account_name = debits[0].account_name if debits else ""

	if len(credits) > 1:
		from_account_name = f"{from_account_name} (+{len(credits) - 1})"
	if len(debits) > 1:
		to_account_name = f"{to_account_name} (+{len(debits) - 1})"

	amount = round(sum(flt(r.debit) for r in debits) or sum(flt(r.credit) for r in credits), 2)

	debit_roots = {r.root_type for r in debits}
	credit_roots = {r.root_type for r in credits}
	debit_liquid = bool(debits) and all(_is_liquid_account(r) for r in debits)
	credit_liquid = bool(credits) and all(_is_liquid_account(r) for r in credits)

	if "Expense" in debit_roots:
		inferred_type = "Expense"
	elif "Income" in credit_roots:
		inferred_type = "Income"
	elif debit_liquid and credit_liquid:
		inferred_type = "Transfer"
	elif credit_liquid and not debit_liquid:
		inferred_type = "Expense"
	elif debit_liquid and not credit_liquid:
		inferred_type = "Income"
	else:
		inferred_type = "Transfer"

	return {
		"name": je.name,
		"voucher": je.name,
		"posting_date": str(je.posting_date),
		"tx_type": inferred_type,
		"type": inferred_type,
		"amount": amount,
		"from_account": from_account,
		"source_account": from_account,
		"from_account_name": from_account_name,
		"to_account": to_account,
		"destination_account": to_account,
		"to_account_name": to_account_name,
		"description": je.user_remark or je.remark or "",
		"attachments_count": attachments_count,
		"tags": _parse_csv_list(je.get("_user_tags")),
	}


def _hydrate_je_list(je_rows):
	if not je_rows:
		return []

	je_names = tuple(r.name for r in je_rows)
	jea_all = frappe.db.sql(
		"""
		SELECT
			jea.parent,
			jea.account,
			jea.debit,
			jea.credit,
			acc.account_name,
			acc.account_type,
			acc.root_type,
			acc.parent_account
		FROM `tabJournal Entry Account` jea
		LEFT JOIN `tabAccount` acc ON jea.account = acc.name
		WHERE jea.parent IN %(je_names)s
		ORDER BY jea.idx ASC
		""",
		{"je_names": je_names},
		as_dict=True,
	)
	jea_by_parent = {}
	for r in jea_all:
		jea_by_parent.setdefault(r.parent, []).append(r)

	att_rows = frappe.db.sql(
		"""
		SELECT attached_to_name, COUNT(*) AS cnt
		FROM `tabFile`
		WHERE attached_to_doctype = 'Journal Entry'
			AND attached_to_name IN %(je_names)s
		GROUP BY attached_to_name
		""",
		{"je_names": je_names},
		as_dict=True,
	)
	att_map = {r.attached_to_name: int(r.cnt) for r in att_rows}

	return [
		_infer_je_details(je, jea_by_parent.get(je.name, []), att_map.get(je.name, 0))
		for je in je_rows
	]


def _apply_tags_to_je(voucher_no, tags_list):
	if not voucher_no or not tags_list:
		return []
	doc_tags = DocTags("Journal Entry")
	for tag in tags_list:
		clean = tag.strip()
		if clean:
			doc_tags.add(voucher_no, clean)
	return _parse_csv_list(doc_tags.get_tags(voucher_no))


@frappe.whitelist()
def get_dashboard_data(from_date=None, to_date=None, month=None):
	"""
	Returns portal dashboard summary, account balances, last-7-days expense bars,
	monthly calendar income/expense totals, top 10 expense categories, todo_transactions, and tag_totals.
	"""
	company, can_write = _resolve_portal_company()

	today_str = today()
	from_date = str(getdate(from_date)) if from_date else "2022-04-01"
	to_date = str(min(getdate(to_date), getdate(today_str))) if to_date else today_str
	target_month = (month or today_str[:7]).strip()
	try:
		month_anchor = getdate(f"{target_month}-01")
	except Exception:
		month_anchor = getdate(today_str)
		target_month = today_str[:7]
	month_start = str(get_first_day(month_anchor))
	month_end = str(get_last_day(month_anchor))

	# 1. Summary totals
	bs_rows = frappe.db.sql(
		"""
		SELECT
			acc.root_type,
			COALESCE(SUM(gle.debit - gle.credit), 0) AS net_dr,
			COALESCE(SUM(CASE WHEN acc.account_type IN ('Bank', 'Cash') THEN gle.debit - gle.credit ELSE 0 END), 0) AS bank_cash_bal
		FROM `tabGL Entry` gle
		INNER JOIN `tabAccount` acc ON gle.account = acc.name
		WHERE gle.company = %(company)s
			AND gle.is_cancelled = 0
			AND gle.posting_date <= %(to_date)s
			AND acc.root_type IN ('Asset', 'Liability')
		GROUP BY acc.root_type
		""",
		{"company": company, "to_date": to_date},
		as_dict=True,
	)

	total_assets = 0.0
	total_liabilities = 0.0
	bank_cash_balance = 0.0
	for r in bs_rows:
		if r.root_type == "Asset":
			total_assets = flt(r.net_dr, 2)
			bank_cash_balance = flt(r.bank_cash_bal, 2)
		elif r.root_type == "Liability":
			total_liabilities = flt(-r.net_dr, 2)

	ie_rows = frappe.db.sql(
		"""
		SELECT
			acc.root_type,
			COALESCE(SUM(gle.credit - gle.debit), 0) AS net_cr
		FROM `tabGL Entry` gle
		INNER JOIN `tabAccount` acc ON gle.account = acc.name
		WHERE gle.company = %(company)s
			AND gle.is_cancelled = 0
			AND gle.posting_date BETWEEN %(from_date)s AND %(to_date)s
			AND acc.root_type IN ('Income', 'Expense')
		GROUP BY acc.root_type
		""",
		{"company": company, "from_date": from_date, "to_date": to_date},
		as_dict=True,
	)

	period_income = 0.0
	period_expense = 0.0
	for r in ie_rows:
		if r.root_type == "Income":
			period_income = flt(r.net_cr, 2)
		elif r.root_type == "Expense":
			period_expense = flt(-r.net_cr, 2)

	summary = {
		"company": company,
		"can_write": can_write,
		"from_date": from_date,
		"to_date": to_date,
		"month": target_month,
		"net_worth": flt(total_assets - total_liabilities, 2),
		"total_assets": total_assets,
		"total_liabilities": total_liabilities,
		"bank_cash_balance": bank_cash_balance,
		"period_income": period_income,
		"period_expense": period_expense,
		"net_surplus": flt(period_income - period_expense, 2),
	}

	# 2. Active Bank, Cash, Credit Card, and Due from/to accounts with live balances
	accounts = frappe.db.sql(
		f"""
		SELECT
			acc.name,
			acc.account_name,
			acc.account_type,
			acc.root_type,
			acc.parent_account,
			ROUND(COALESCE(SUM(gle.debit - gle.credit), 0), 2) AS balance,
			COUNT(gle.name) AS tx_count,
			MAX(gle.posting_date) AS last_date
		FROM `tabAccount` acc
		LEFT JOIN `tabGL Entry` gle
			ON gle.account = acc.name
			AND gle.company = %(company)s
			AND gle.is_cancelled = 0
			AND gle.posting_date <= %(to_date)s
		WHERE acc.company = %(company)s
			AND acc.is_group = 0
			AND acc.disabled = 0
			AND (
				acc.account_type IN ('Bank', 'Cash')
				OR {CREDIT_CARD_SQL}
				OR acc.account_name LIKE 'Due from%%'
				OR acc.account_name LIKE 'Due to%%'
			)
		GROUP BY acc.name, acc.account_name, acc.account_type, acc.root_type, acc.parent_account
		ORDER BY acc.account_type DESC, acc.account_name ASC
		""",
		{"company": company, "to_date": to_date},
		as_dict=True,
	)

	for acc in accounts:
		acc["group_label"] = _classify_account_group(acc)
		acc["balance"] = flt(acc["balance"], 2)
		acc["last_date"] = str(acc["last_date"]) if acc.get("last_date") else None

	# 3. Last 7 days daily expense totals (zero-filled)
	week_start = str(add_days(today_str, -6))
	weekly_raw = frappe.db.sql(
		"""
		SELECT
			gle.posting_date,
			ROUND(COALESCE(SUM(gle.debit - gle.credit), 0), 2) AS amount
		FROM `tabGL Entry` gle
		INNER JOIN `tabAccount` acc ON gle.account = acc.name
		WHERE gle.company = %(company)s
			AND gle.is_cancelled = 0
			AND acc.root_type = 'Expense'
			AND gle.posting_date BETWEEN %(week_start)s AND %(today)s
		GROUP BY gle.posting_date
		ORDER BY gle.posting_date ASC
		""",
		{"company": company, "week_start": week_start, "today": today_str},
		as_dict=True,
	)
	weekly_map = {str(r.posting_date): flt(r.amount, 2) for r in weekly_raw}
	weekly_expenses = [
		{"date": str(add_days(week_start, d)), "amount": weekly_map.get(str(add_days(week_start, d)), 0.0)}
		for d in range(7)
	]

	# 4. Calendar days (daily income & expense for target month, capped to today)
	cal_end = str(min(getdate(month_end), getdate(today_str)))
	cal_raw = []
	if getdate(month_start) <= getdate(cal_end):
		cal_raw = frappe.db.sql(
			"""
			SELECT
				gle.posting_date,
				ROUND(COALESCE(SUM(CASE WHEN acc.root_type = 'Income' THEN gle.credit - gle.debit ELSE 0 END), 0), 2) AS income,
				ROUND(COALESCE(SUM(CASE WHEN acc.root_type = 'Expense' THEN gle.debit - gle.credit ELSE 0 END), 0), 2) AS expense
			FROM `tabGL Entry` gle
			INNER JOIN `tabAccount` acc ON gle.account = acc.name
			WHERE gle.company = %(company)s
				AND gle.is_cancelled = 0
				AND acc.root_type IN ('Income', 'Expense')
				AND gle.posting_date BETWEEN %(month_start)s AND %(month_end)s
			GROUP BY gle.posting_date
			ORDER BY gle.posting_date ASC
			""",
			{"company": company, "month_start": month_start, "month_end": cal_end},
			as_dict=True,
		)
	calendar_days = [
		{"date": str(r.posting_date), "income": flt(r.income, 2), "expense": flt(r.expense, 2)}
		for r in cal_raw
	]

	# 5. Top 10 expense categories for [from_date, to_date]
	top_categories = frappe.db.sql(
		"""
		SELECT
			acc.name AS account,
			acc.account_name,
			ROUND(SUM(gle.debit - gle.credit), 2) AS amount
		FROM `tabGL Entry` gle
		INNER JOIN `tabAccount` acc ON gle.account = acc.name
		WHERE gle.company = %(company)s
			AND gle.is_cancelled = 0
			AND acc.root_type = 'Expense'
			AND gle.posting_date BETWEEN %(from_date)s AND %(to_date)s
		GROUP BY acc.name, acc.account_name
		HAVING amount > 0.01
		ORDER BY amount DESC
		LIMIT 10
		""",
		{"company": company, "from_date": from_date, "to_date": to_date},
		as_dict=True,
	)
	for cat in top_categories:
		cat["amount"] = flt(cat["amount"], 2)

	# 5b. Full Income & Expenditure category breakdown for interactive pie charts
	company_abbr = frappe.db.get_value("Company", company, "abbr") or ""
	abbr_suffix = f" - {company_abbr}" if company_abbr else ""
	cat_rows = frappe.db.sql(
		'''
		SELECT
			acc.name AS account,
			acc.account_name,
			acc.root_type,
			ROUND(
				SUM(
					CASE
						WHEN acc.root_type = 'Income' THEN gle.credit - gle.debit
						ELSE gle.debit - gle.credit
					END
				),
				2
			) AS amount
		FROM `tabGL Entry` gle
		INNER JOIN `tabAccount` acc ON gle.account = acc.name
		WHERE gle.company = %(company)s
			AND gle.is_cancelled = 0
			AND acc.root_type IN ('Income', 'Expense')
			AND gle.posting_date BETWEEN %(from_date)s AND %(to_date)s
			AND gle.posting_date <= %(today_str)s
		GROUP BY acc.name, acc.account_name, acc.root_type
		HAVING amount > 0.01
		ORDER BY amount DESC, acc.account_name ASC
		''',
		{"company": company, "from_date": from_date, "to_date": to_date, "today_str": today_str},
		as_dict=True,
	)

	income_categories = _format_category_rows([r for r in cat_rows if r.root_type == "Income"], abbr_suffix)
	expense_categories = _format_category_rows([r for r in cat_rows if r.root_type == "Expense"], abbr_suffix)
	monthly_categories = _compute_monthly_categories(company, today_str, abbr_suffix)

	# 6. Portal Tag metadata, To-Do transactions, and period tag_totals
	portal_tags = frappe.get_all(
		"Family Portal Tag",
		filters={"company": company},
		fields=["name", "tag_name", "is_todo", "color"],
		order_by="is_todo desc, tag_name asc",
	)
	tag_meta = {t.tag_name: t for t in portal_tags}
	todo_tag_names = [t.tag_name for t in portal_tags if cint(t.is_todo) == 1]

	todo_transactions = []
	if todo_tag_names:
		like_clauses = []
		todo_params = {"company": company, "to_date": to_date}
		for idx, t_name in enumerate(todo_tag_names):
			key = f"t{idx}"
			like_clauses.append(f"CONCAT(',', COALESCE(je._user_tags, ''), ',') LIKE CONCAT('%,', %({key})s, ',%')")
			todo_params[key] = t_name
		todo_je_rows = frappe.db.sql(
			f"""
			SELECT je.name, je.posting_date, je.user_remark, je.remark, je._user_tags
			FROM `tabJournal Entry` je
			WHERE je.company = %(company)s
				AND je.docstatus = 1
				AND je.posting_date <= %(to_date)s
				AND ({' OR '.join(like_clauses)})
			ORDER BY je.posting_date DESC, je.creation DESC
			LIMIT 20
			""",
			todo_params,
			as_dict=True,
		)
		todo_transactions = _hydrate_je_list(todo_je_rows)

	tagged_period_rows = frappe.db.sql(
		"""
		SELECT
			je.name,
			je._user_tags,
			COALESCE(SUM(jea.debit), 0) AS je_amount
		FROM `tabJournal Entry` je
		INNER JOIN `tabJournal Entry Account` jea ON jea.parent = je.name
		WHERE je.company = %(company)s
			AND je.docstatus = 1
			AND je.posting_date BETWEEN %(from_date)s AND %(to_date)s
			AND COALESCE(je._user_tags, '') != ''
		GROUP BY je.name, je._user_tags
		""",
		{"company": company, "from_date": from_date, "to_date": to_date},
		as_dict=True,
	)

	tag_agg = {}
	for row in tagged_period_rows:
		amt = flt(row.je_amount, 2)
		for t_name in _parse_csv_list(row.get("_user_tags")):
			entry = tag_agg.setdefault(
				t_name,
				{
					"tag_name": t_name,
					"amount": 0.0,
					"tx_count": 0,
					"is_todo": cint(tag_meta[t_name].is_todo) if t_name in tag_meta else 0,
					"color": tag_meta[t_name].color if t_name in tag_meta else "#2563eb",
				},
			)
			entry["amount"] = flt(entry["amount"] + amt, 2)
			entry["tx_count"] += 1

	tag_totals = sorted(tag_agg.values(), key=lambda x: (-x["amount"], x["tag_name"]))

	monthly_trend = _compute_monthly_trend(company, today_str)
	budgets = _compute_budgets(company, target_month)
	preferences = _get_user_preferences(frappe.session.user, company)

	return {
		"summary": summary,
		"accounts": accounts,
		"weekly_expenses": weekly_expenses,
		"monthly_trend": monthly_trend,
		"calendar_days": calendar_days,
		"top_categories": top_categories,
		"income_categories": income_categories,
		"expense_categories": expense_categories,
		"monthly_categories": monthly_categories,
		"todo_transactions": todo_transactions,
		"tag_totals": tag_totals,
		"budgets": budgets,
		"preferences": preferences,
	}


@frappe.whitelist()
def get_transactions(search="", account="", tx_type="", tag="", from_date=None, to_date=None, limit=50, offset=0):
	"""
	Queries non-cancelled Journal Entries for the locked company and returns structured transactions with tags.
	"""
	company, can_write = _resolve_portal_company()

	limit = min(max(cint(limit) or 50, 1), 200)
	offset = max(cint(offset) or 0, 0)
	search = (search or "").strip()
	account = (account or "").strip()
	tx_type = _normalize_tx_type(tx_type)
	tag = (tag or "").strip()

	conds = ["je.company = %(company)s", "je.docstatus = 1"]
	params = {"company": company}

	if from_date:
		conds.append("je.posting_date >= %(from_date)s")
		params["from_date"] = str(getdate(from_date))
	conds.append("je.posting_date <= %(to_date)s")
	params["to_date"] = str(getdate(to_date)) if to_date else today()
	if account:
		conds.append(
			"EXISTS (SELECT 1 FROM `tabJournal Entry Account` j WHERE j.parent = je.name AND j.account = %(account)s)"
		)
		params["account"] = account
	if tag:
		conds.append("CONCAT(',', COALESCE(je._user_tags, ''), ',') LIKE CONCAT('%,', %(tag)s, ',%')")
		params["tag"] = tag
	if search:
		conds.append(
			"""(
				je.name LIKE %(search)s
				OR COALESCE(je.user_remark, '') LIKE %(search)s
				OR COALESCE(je.remark, '') LIKE %(search)s
				OR COALESCE(je._user_tags, '') LIKE %(search)s
				OR EXISTS (
					SELECT 1 FROM `tabJournal Entry Account` j
					WHERE j.parent = je.name AND j.account LIKE %(search)s
				)
			)"""
		)
		params["search"] = f"%{search}%"

	where_sql = " AND ".join(conds)

	if tx_type:
		je_rows = frappe.db.sql(
			f"""
			SELECT je.name, je.posting_date, je.user_remark, je.remark, je._user_tags
			FROM `tabJournal Entry` je
			WHERE {where_sql}
			ORDER BY je.posting_date DESC, je.creation DESC
			""",
			params,
			as_dict=True,
		)
		hydrated = _hydrate_je_list(je_rows)
		filtered = [item for item in hydrated if item["tx_type"] == tx_type]
		total_count = len(filtered)
		items = filtered[offset : offset + limit]
	else:
		total_count = frappe.db.sql(
			f"SELECT COUNT(*) FROM `tabJournal Entry` je WHERE {where_sql}",
			params,
		)[0][0]
		params_page = dict(params, limit=limit, offset=offset)
		je_rows = frappe.db.sql(
			f"""
			SELECT je.name, je.posting_date, je.user_remark, je.remark, je._user_tags
			FROM `tabJournal Entry` je
			WHERE {where_sql}
			ORDER BY je.posting_date DESC, je.creation DESC
			LIMIT %(limit)s OFFSET %(offset)s
			""",
			params_page,
			as_dict=True,
		)
		items = _hydrate_je_list(je_rows)

	return {
		"items": items,
		"total": int(total_count),
		"limit": limit,
		"offset": offset,
		"can_write": can_write,
	}


@frappe.whitelist()
def get_form_options():
	"""
	Returns non-group accounts for company grouped by category, plus family_companies if primary.
	"""
	company, can_write = _resolve_portal_company()

	rows = frappe.db.sql(
		"""
		SELECT name, account_name, account_type, root_type, parent_account
		FROM `tabAccount`
		WHERE company = %(company)s
			AND is_group = 0
			AND disabled = 0
		ORDER BY account_name ASC
		""",
		{"company": company},
		as_dict=True,
	)

	bank_cash = []
	expense = []
	income = []
	other_assets_liabilities = []

	for acc in rows:
		item = {
			"name": acc.name,
			"account_name": acc.account_name,
			"account_type": acc.account_type or "",
			"root_type": acc.root_type or "",
		}
		if _is_liquid_account(acc):
			bank_cash.append(item)
		elif acc.root_type == "Expense":
			expense.append(item)
		elif acc.root_type == "Income":
			income.append(item)
		elif acc.root_type in ("Asset", "Liability", "Equity"):
			other_assets_liabilities.append(item)

	settings = frappe.get_single("Family Accounting Settings")
	family_companies = []
	if company == settings.primary_company:
		for m in settings.company_mappings:
			if not m.family_company:
				continue
			family_companies.append({
				"family_company": m.family_company,
				"primary_clearing_account": m.primary_clearing_account,
				"counterparty_clearing_account": m.counterparty_clearing_account,
				"default_expense_account": m.default_expense_account,
				"default_settlement_account": m.default_settlement_account,
			})

	return {
		"company": company,
		"can_write": can_write,
		"bank_cash": bank_cash,
		"expense": expense,
		"income": income,
		"other_assets_liabilities": other_assets_liabilities,
		"family_companies": family_companies,
	}


def _validate_account_belongs_to(account, expected_company, label):
	if not account:
		frappe.throw(_("{0} is required.").format(label))
	row = frappe.db.get_value(
		"Account",
		account,
		["name", "company", "is_group", "root_type"],
		as_dict=True,
	)
	if not row:
		frappe.throw(_("{0} '{1}' does not exist.").format(label, account))
	if row.is_group:
		frappe.throw(_("{0} '{1}' cannot be a group account.").format(label, account))
	if row.company != expected_company:
		frappe.throw(
			_("{0} '{1}' does not belong to Company '{2}'.").format(label, account, expected_company),
			frappe.PermissionError,
		)
	return row


@frappe.whitelist(methods=["POST"])
def create_transaction(
	tx_type,
	posting_date,
	amount,
	from_account,
	to_account=None,
	description="",
	family_company=None,
	family_tx_type=None,
	tags="",
):
	"""
	Creates and submits a Family Transaction, Quick Expense, or Journal Entry
	strictly scoped to the user's locked Company, and applies optional tags.
	"""
	company, can_write = _resolve_portal_company()
	if not can_write:
		frappe.throw(_("You do not have write permission on this portal."), frappe.PermissionError)

	tx_type = _normalize_tx_type(tx_type)
	if tx_type not in ("Expense", "Income", "Transfer", "Family"):
		frappe.throw(_("Invalid transaction type: {0}").format(tx_type))

	amt = flt(amount)
	if amt <= 0:
		frappe.throw(_("Amount must be greater than zero."))

	posting_date = str(getdate(posting_date))
	description = (description or "").strip()
	tags_list = _parse_csv_list(tags)

	if tx_type == "Family" and family_company:
		settings = frappe.get_single("Family Accounting Settings")
		if company != settings.primary_company:
			frappe.throw(
				_("Inter-company Family Transactions can only be created from {0}.").format(
					settings.primary_company
				),
				frappe.PermissionError,
			)
		mapped = [m.family_company for m in settings.company_mappings if m.family_company]
		if family_company not in mapped:
			frappe.throw(_("Invalid Family Company '{0}'.").format(family_company))

		_validate_account_belongs_to(from_account, company, "Funding Account")
		if to_account:
			_validate_account_belongs_to(to_account, family_company, "Counterparty Account")

		ft = frappe.new_doc("Family Transaction")
		ft.posting_date = posting_date
		ft.transaction_type = family_tx_type or "Expense Paid on Behalf"
		ft.primary_company = company
		ft.funding_account = from_account
		ft.family_company = family_company
		if to_account:
			ft.counterparty_account = to_account
		ft.amount = amt
		ft.description = description
		ft.flags.ignore_permissions = True
		ft.insert()
		ft.submit()

		applied_tags = _apply_tags_to_je(ft.primary_journal_entry, tags_list)
		frappe.db.commit()

		return {
			"doctype": "Family Transaction",
			"name": ft.name,
			"journal_entry": ft.primary_journal_entry,
			"counterparty_journal_entry": ft.counterparty_journal_entry,
			"tags": applied_tags,
		}

	_validate_account_belongs_to(from_account, company, "From Account")
	to_acc_doc = _validate_account_belongs_to(to_account, company, "To Account")

	if from_account == to_account:
		frappe.throw(_("From Account and To Account cannot be the same."))

	if tx_type == "Expense" and to_acc_doc.root_type == "Expense":
		qe = frappe.new_doc("Quick Expense")
		qe.posting_date = posting_date
		qe.company = company
		qe.paid_from = from_account
		qe.expense_account = to_account
		qe.amount = amt
		qe.description = description or to_account
		qe.flags.ignore_permissions = True
		qe.insert()
		qe.submit()

		applied_tags = _apply_tags_to_je(qe.journal_entry, tags_list)
		frappe.db.commit()

		return {
			"doctype": "Quick Expense",
			"name": qe.name,
			"journal_entry": qe.journal_entry,
			"tags": applied_tags,
		}

	je = frappe.new_doc("Journal Entry")
	je.voucher_type = "Journal Entry"
	je.company = company
	je.posting_date = posting_date
	je.user_remark = description or f"{tx_type}: {from_account} -> {to_account}"
	je.append(
		"accounts",
		{
			"account": to_account,
			"debit_in_account_currency": amt,
			"credit_in_account_currency": 0,
		},
	)
	je.append(
		"accounts",
		{
			"account": from_account,
			"debit_in_account_currency": 0,
			"credit_in_account_currency": amt,
		},
	)
	je.flags.ignore_permissions = True
	je.insert()
	je.submit()

	applied_tags = _apply_tags_to_je(je.name, tags_list)
	frappe.db.commit()

	return {
		"doctype": "Journal Entry",
		"name": je.name,
		"journal_entry": je.name,
		"tags": applied_tags,
	}


@frappe.whitelist(methods=["POST"])
def update_transaction(
	voucher,
	posting_date=None,
	amount=None,
	source_account=None,
	destination_account=None,
	description=None,
	tags=None,
):
	"""
	Updates a submitted Journal Entry by cancelling it and creating/submitting an updated
	(amended) Journal Entry with the revised posting_date, amount, accounts, description, and tags.
	"""
	company, can_write = _resolve_portal_company()
	if not can_write:
		frappe.throw(_("You do not have write permission on this portal."), frappe.PermissionError)

	voucher = (voucher or "").strip()
	if not voucher or not frappe.db.exists("Journal Entry", voucher):
		frappe.throw(_("Journal Entry '{0}' does not exist.").format(voucher))

	old_je = frappe.get_doc("Journal Entry", voucher)
	if old_je.company != company:
		frappe.throw(
			_("Journal Entry '{0}' does not belong to Company '{1}'.").format(voucher, company),
			frappe.PermissionError,
		)
	if old_je.docstatus != 1:
		frappe.throw(_("Journal Entry '{0}' is not submitted.").format(voucher))

	ft_hit = frappe.db.sql(
		"""
		SELECT name FROM `tabFamily Transaction`
		WHERE docstatus = 1 AND (primary_journal_entry = %(v)s OR counterparty_journal_entry = %(v)s)
		LIMIT 1
		""",
		{"v": voucher},
	)
	if ft_hit:
		frappe.throw(_("Inter-company Family Transactions cannot be edited inline; cancel and recreate instead."))

	hydrated_old = _hydrate_je_list([old_je])[0]
	new_date = str(getdate(posting_date)) if posting_date else str(old_je.posting_date)
	new_amt = flt(amount) if amount is not None and str(amount).strip() != "" else flt(hydrated_old.get("amount"))
	if new_amt <= 0:
		frappe.throw(_("Amount must be greater than zero."))

	new_src = (source_account or hydrated_old.get("from_account") or "").strip()
	new_dst = (destination_account or hydrated_old.get("to_account") or "").strip()
	_validate_account_belongs_to(new_src, company, "Source Account")
	_validate_account_belongs_to(new_dst, company, "Destination Account")
	if new_src == new_dst:
		frappe.throw(_("Source Account and Destination Account cannot be the same."))

	new_desc = (old_je.user_remark or old_je.remark or "") if description is None else str(description).strip()
	tags_list = hydrated_old.get("tags", []) if tags is None else _parse_csv_list(tags)

	qe_name = frappe.db.get_value(
		"Quick Expense",
		{"journal_entry": voucher, "docstatus": 1, "company": company},
		"name",
	)
	if qe_name:
		qe = frappe.get_doc("Quick Expense", qe_name)
		qe.flags.ignore_permissions = True
		qe.cancel()
	else:
		old_je.flags.ignore_permissions = True
		old_je.cancel()

	new_je = frappe.new_doc("Journal Entry")
	new_je.voucher_type = old_je.voucher_type or "Journal Entry"
	new_je.company = company
	new_je.posting_date = new_date
	new_je.user_remark = new_desc or f"{new_src} -> {new_dst}"
	if not qe_name:
		new_je.amended_from = old_je.name
	new_je.append(
		"accounts",
		{
			"account": new_dst,
			"debit_in_account_currency": new_amt,
			"credit_in_account_currency": 0,
		},
	)
	new_je.append(
		"accounts",
		{
			"account": new_src,
			"debit_in_account_currency": 0,
			"credit_in_account_currency": new_amt,
		},
	)
	new_je.flags.ignore_permissions = True
	new_je.insert()
	new_je.submit()

	_apply_tags_to_je(new_je.name, tags_list)
	frappe.db.commit()

	updated_tx = _hydrate_je_list([new_je])[0]
	return {
		"ok": True,
		"old_voucher": voucher,
		"voucher": new_je.name,
		"transaction": updated_tx,
	}


@frappe.whitelist(methods=["POST"])
def delete_transaction(voucher=None, voucher_no=None):
	"""
	Cancels a submitted Journal Entry (or its linked Quick Expense / Family Transaction)
	strictly scoped to the user's locked Company. Requires Family Portal User or System Manager.
	"""
	company, can_write = _resolve_portal_company()
	if not can_write:
		frappe.throw(_("You do not have write permission on this portal."), frappe.PermissionError)

	voucher_no = (voucher or voucher_no or "").strip()
	if not voucher_no or not frappe.db.exists("Journal Entry", voucher_no):
		frappe.throw(_("Journal Entry '{0}' does not exist.").format(voucher_no))

	je = frappe.get_doc("Journal Entry", voucher_no)
	if je.company != company:
		frappe.throw(
			_("Journal Entry '{0}' does not belong to Company '{1}'.").format(voucher_no, company),
			frappe.PermissionError,
		)
	if je.docstatus != 1:
		frappe.throw(_("Journal Entry '{0}' is not submitted.").format(voucher_no))

	qe_name = frappe.db.get_value(
		"Quick Expense",
		{"journal_entry": voucher_no, "docstatus": 1, "company": company},
		"name",
	)
	if qe_name:
		qe = frappe.get_doc("Quick Expense", qe_name)
		qe.flags.ignore_permissions = True
		qe.cancel()
		frappe.db.commit()
		return {
			"ok": True,
			"status": "cancelled",
			"doctype": "Quick Expense",
			"name": qe.name,
			"voucher": voucher_no,
			"voucher_no": voucher_no,
		}

	ft_name = frappe.db.sql(
		"""
		SELECT name, primary_company
		FROM `tabFamily Transaction`
		WHERE docstatus = 1
			AND (primary_journal_entry = %(v)s OR counterparty_journal_entry = %(v)s)
		LIMIT 1
		""",
		{"v": voucher_no},
		as_dict=True,
	)
	if ft_name:
		if ft_name[0].primary_company != company:
			frappe.throw(
				_("Only the primary company ({0}) can cancel inter-company transaction {1}.").format(
					ft_name[0].primary_company, ft_name[0].name
				),
				frappe.PermissionError,
			)
		ft = frappe.get_doc("Family Transaction", ft_name[0].name)
		ft.flags.ignore_permissions = True
		ft.cancel()
		frappe.db.commit()
		return {
			"ok": True,
			"status": "cancelled",
			"doctype": "Family Transaction",
			"name": ft.name,
			"voucher": voucher_no,
			"voucher_no": voucher_no,
		}

	je.flags.ignore_permissions = True
	je.cancel()
	frappe.db.commit()
	return {
		"ok": True,
		"status": "cancelled",
		"doctype": "Journal Entry",
		"name": je.name,
		"voucher": voucher_no,
		"voucher_no": voucher_no,
	}


# --- Templates ---

def _serialize_template(row):
	d = dict(row)
	d["tx_type"] = _normalize_tx_type(d.get("tx_type") or "Expense")
	d["default_amount"] = flt(d.get("default_amount"), 2)
	d["tags_list"] = _parse_csv_list(d.get("default_tags"))
	d["aliases_list"] = _parse_csv_list(d.get("audio_aliases"), split_newlines=True)
	return d


@frappe.whitelist()
def get_templates():
	"""Returns all Family Transaction Template records for the user's locked Company."""
	company, can_write = _resolve_portal_company()
	rows = frappe.get_all(
		"Family Transaction Template",
		filters={"company": company},
		fields=[
			"name",
			"company",
			"template_name",
			"tx_type",
			"from_account",
			"to_account",
			"family_company",
			"family_tx_type",
			"default_amount",
			"description",
			"default_tags",
			"audio_aliases",
		],
		order_by="template_name asc",
	)
	return {
		"items": [_serialize_template(r) for r in rows],
		"can_write": can_write,
	}


@frappe.whitelist(methods=["POST"])
def save_template(
	template_name,
	tx_type="Withdrawal",
	from_account=None,
	to_account=None,
	family_company=None,
	family_tx_type=None,
	default_amount=0,
	description="",
	default_tags="",
	audio_aliases="",
	name=None,
):
	"""Creates or updates a Family Transaction Template strictly scoped to the user's locked Company."""
	company, can_write = _resolve_portal_company()
	if not can_write:
		frappe.throw(_("You do not have write permission on this portal."), frappe.PermissionError)

	template_name = (template_name or "").strip()
	if not template_name:
		frappe.throw(_("Template Name is required."))

	tx_type = _normalize_tx_type(tx_type or "Expense")
	if tx_type not in ("Expense", "Income", "Transfer", "Family"):
		frappe.throw(_("Invalid transaction type: {0}").format(tx_type))

	if from_account:
		_validate_account_belongs_to(from_account, company, "From Account")
	if to_account:
		target_company = family_company if (tx_type == "Family" and family_company) else company
		_validate_account_belongs_to(to_account, target_company, "To Account")

	if family_company:
		settings = frappe.get_single("Family Accounting Settings")
		mapped = [m.family_company for m in settings.company_mappings if m.family_company]
		if family_company not in mapped:
			frappe.throw(_("Invalid Family Company '{0}'.").format(family_company))

	if name:
		if not frappe.db.exists("Family Transaction Template", name):
			frappe.throw(_("Template '{0}' does not exist.").format(name))
		doc = frappe.get_doc("Family Transaction Template", name)
		if doc.company != company:
			frappe.throw(_("Template does not belong to Company '{0}'.").format(company), frappe.PermissionError)
	else:
		doc = frappe.new_doc("Family Transaction Template")
		doc.company = company

	doc.template_name = template_name
	doc.tx_type = tx_type
	doc.from_account = from_account or None
	doc.to_account = to_account or None
	doc.family_company = family_company or None
	doc.family_tx_type = family_tx_type or None
	doc.default_amount = flt(default_amount)
	doc.description = (description or "").strip()
	doc.default_tags = ", ".join(_parse_csv_list(default_tags))
	doc.audio_aliases = (audio_aliases or "").strip()
	doc.flags.ignore_permissions = True
	doc.save()
	frappe.db.commit()

	return _serialize_template(doc.as_dict())


@frappe.whitelist(methods=["POST"])
def delete_template(name):
	"""Deletes a Family Transaction Template belonging to the user's locked Company."""
	company, can_write = _resolve_portal_company()
	if not can_write:
		frappe.throw(_("You do not have write permission on this portal."), frappe.PermissionError)

	if not name or not frappe.db.exists("Family Transaction Template", name):
		frappe.throw(_("Template '{0}' does not exist.").format(name))
	doc_company = frappe.db.get_value("Family Transaction Template", name, "company")
	if doc_company != company:
		frappe.throw(_("Template does not belong to Company '{0}'.").format(company), frappe.PermissionError)

	frappe.delete_doc("Family Transaction Template", name, ignore_permissions=True)
	frappe.db.commit()
	return {"status": "deleted", "name": name}


# --- Tags & To-Do ---

@frappe.whitelist()
def get_tags():
	"""Returns all Family Portal Tag records for the user's locked Company."""
	company, can_write = _resolve_portal_company()
	rows = frappe.get_all(
		"Family Portal Tag",
		filters={"company": company},
		fields=["name", "company", "tag_name", "is_todo", "color"],
		order_by="is_todo desc, tag_name asc",
	)
	for r in rows:
		r["is_todo"] = cint(r.get("is_todo"))
	return {"items": rows, "can_write": can_write}


@frappe.whitelist(methods=["POST"])
def save_tag(tag_name, is_todo=0, color="#2563eb", name=None):
	"""Creates or updates a Family Portal Tag (unique per company + tag_name)."""
	company, can_write = _resolve_portal_company()
	if not can_write:
		frappe.throw(_("You do not have write permission on this portal."), frappe.PermissionError)

	tag_name = (tag_name or "").strip()
	if not tag_name or "," in tag_name:
		frappe.throw(_("Tag Name is required and cannot contain commas."))

	dup_filters = {"company": company, "tag_name": tag_name}
	if name:
		dup_filters["name"] = ["!=", name]
	if frappe.db.exists("Family Portal Tag", dup_filters):
		frappe.throw(_("Tag '{0}' already exists for {1}.").format(tag_name, company))

	if name:
		if not frappe.db.exists("Family Portal Tag", name):
			frappe.throw(_("Tag '{0}' does not exist.").format(name))
		doc = frappe.get_doc("Family Portal Tag", name)
		if doc.company != company:
			frappe.throw(_("Tag does not belong to Company '{0}'.").format(company), frappe.PermissionError)
	else:
		doc = frappe.new_doc("Family Portal Tag")
		doc.company = company

	doc.tag_name = tag_name
	doc.is_todo = 1 if cint(is_todo) else 0
	doc.color = (color or "#2563eb").strip()
	doc.flags.ignore_permissions = True
	doc.save()
	frappe.db.commit()

	return {
		"name": doc.name,
		"company": doc.company,
		"tag_name": doc.tag_name,
		"is_todo": cint(doc.is_todo),
		"color": doc.color,
	}


@frappe.whitelist(methods=["POST"])
def delete_tag(name):
	"""Deletes a Family Portal Tag belonging to the user's locked Company."""
	company, can_write = _resolve_portal_company()
	if not can_write:
		frappe.throw(_("You do not have write permission on this portal."), frappe.PermissionError)

	if not name or not frappe.db.exists("Family Portal Tag", name):
		frappe.throw(_("Tag '{0}' does not exist.").format(name))
	doc_company = frappe.db.get_value("Family Portal Tag", name, "company")
	if doc_company != company:
		frappe.throw(_("Tag does not belong to Company '{0}'.").format(company), frappe.PermissionError)

	frappe.delete_doc("Family Portal Tag", name, ignore_permissions=True)
	frappe.db.commit()
	return {"status": "deleted", "name": name}


@frappe.whitelist(methods=["POST"])
def toggle_transaction_tag(voucher_no, tag_name, action="toggle"):
	"""Adds or removes tag_name on Journal Entry._user_tags for a JE in the locked Company."""
	company, can_write = _resolve_portal_company()
	if not can_write:
		frappe.throw(_("You do not have write permission on this portal."), frappe.PermissionError)

	voucher_no = (voucher_no or "").strip()
	tag_name = (tag_name or "").strip()
	action = (action or "toggle").strip().lower()

	if not tag_name or "," in tag_name:
		frappe.throw(_("Valid tag_name without commas is required."))
	if action not in ("toggle", "add", "remove"):
		frappe.throw(_("Invalid action: {0}").format(action))
	if not voucher_no or not frappe.db.exists("Journal Entry", voucher_no):
		frappe.throw(_("Journal Entry '{0}' does not exist.").format(voucher_no))

	je_company, docstatus = frappe.db.get_value("Journal Entry", voucher_no, ["company", "docstatus"])
	if je_company != company:
		frappe.throw(
			_("Journal Entry '{0}' does not belong to Company '{1}'.").format(voucher_no, company),
			frappe.PermissionError,
		)
	if docstatus != 1:
		frappe.throw(_("Journal Entry '{0}' is not submitted.").format(voucher_no))

	doc_tags = DocTags("Journal Entry")
	current = _parse_csv_list(doc_tags.get_tags(voucher_no))
	has_tag = any(t.lower() == tag_name.lower() for t in current)

	if action == "add" or (action == "toggle" and not has_tag):
		doc_tags.add(voucher_no, tag_name)
	elif action == "remove" or (action == "toggle" and has_tag):
		doc_tags.remove(voucher_no, tag_name)

	frappe.db.commit()
	updated = _parse_csv_list(doc_tags.get_tags(voucher_no))
	return {"voucher_no": voucher_no, "tags": updated}



# --- Phase 3A: Accounts Overview, Balance Adjust, Budgets & Preferences ---

DEFAULT_QUICK_AMOUNTS = "100,250,500,1000,2000,5000"


def _parse_json_list(val):
	if not val:
		return []
	if isinstance(val, list):
		return [str(x).strip() for x in val if str(x).strip()]
	try:
		parsed = json.loads(val)
		if isinstance(parsed, list):
			return [str(x).strip() for x in parsed if str(x).strip()]
	except Exception:
		pass
	return [x.strip() for x in str(val).split(",") if x.strip()]


def _get_user_preferences(user, company):
	row = frappe.db.get_value(
		"Family Portal Preference",
		{"user": user, "company": company},
		["name", "quick_amounts", "default_from_account", "pinned_accounts", "hidden_cards"],
		as_dict=True,
	)
	quick_str = (row.quick_amounts if row and row.quick_amounts else DEFAULT_QUICK_AMOUNTS).strip()
	quick_list = []
	for part in quick_str.split(","):
		try:
			num = flt(part.strip())
			if num > 0:
				quick_list.append(num)
		except Exception:
			pass
	if not quick_list:
		quick_list = [100, 250, 500, 1000, 2000, 5000]

	return {
		"name": row.name if row else None,
		"user": user,
		"company": company,
		"quick_amounts": quick_str,
		"quick_amounts_list": quick_list,
		"default_from_account": (row.default_from_account if row else None) or "",
		"pinned_accounts": _parse_json_list(row.pinned_accounts if row else None),
		"hidden_cards": _parse_json_list(row.hidden_cards if row else None),
	}


def _find_adjustment_account(company, exclude_account):
	preferred = [
		"Reconciliation Adjustments",
		"Temporary Opening",
		"Opening Balance Equity",
		"Round Off",
		"Miscellaneous Expenses",
	]
	for acc_name in preferred:
		match = frappe.db.get_value(
			"Account",
			{"company": company, "account_name": acc_name, "is_group": 0, "name": ["!=", exclude_account]},
			"name",
		)
		if match:
			return match
	fallback = frappe.db.sql(
		"""
		SELECT name FROM `tabAccount`
		WHERE company = %(company)s
			AND is_group = 0
			AND disabled = 0
			AND name != %(excl)s
			AND root_type IN ('Equity', 'Expense')
		ORDER BY FIELD(root_type, 'Equity', 'Expense'), account_name ASC
		LIMIT 1
		""",
		{"company": company, "excl": exclude_account},
	)
	if fallback:
		return fallback[0][0]
	frappe.throw(_("No adjustment / offset account found in Company '{0}'.").format(company))


MONTH_ABBR = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _compute_monthly_trend(company, today_str):
	"""Computes last 12 calendar months ending in today_str's month, capped at posting_date <= today_str."""
	today_d = getdate(today_str)
	end_year, end_month = today_d.year, today_d.month

	months_12 = []
	for back in range(11, -1, -1):
		total_m = (end_year * 12 + (end_month - 1)) - back
		y, m0 = divmod(total_m, 12)
		m = m0 + 1
		months_12.append((y, m, f"{y:04d}-{m:02d}"))

	trend_start = f"{months_12[0][0]:04d}-{months_12[0][1]:02d}-01"
	rows = frappe.db.sql(
		"""
		SELECT
			DATE_FORMAT(gle.posting_date, '%%Y-%%m') AS ym,
			acc.root_type,
			COALESCE(SUM(CASE WHEN acc.root_type = 'Income' THEN gle.credit - gle.debit ELSE gle.debit - gle.credit END), 0) AS amt
		FROM `tabGL Entry` gle
		INNER JOIN `tabAccount` acc ON gle.account = acc.name
		WHERE gle.company = %(company)s
			AND gle.is_cancelled = 0
			AND acc.root_type IN ('Income', 'Expense')
			AND gle.posting_date BETWEEN %(trend_start)s AND %(today)s
		GROUP BY DATE_FORMAT(gle.posting_date, '%%Y-%%m'), acc.root_type
		""",
		{"company": company, "trend_start": trend_start, "today": today_str},
		as_dict=True,
	)

	agg = {}
	for r in rows:
		ym = r.get("ym")
		if not ym:
			continue
		entry = agg.setdefault(ym, {"Income": 0.0, "Expense": 0.0})
		if r.get("root_type") in ("Income", "Expense"):
			entry[r["root_type"]] = flt(r.get("amt"), 2)

	trend = []
	for y, m, ym in months_12:
		inc = round(agg.get(ym, {}).get("Income", 0.0), 2)
		exp = round(agg.get(ym, {}).get("Expense", 0.0), 2)
		surplus = round(inc - exp, 2)
		trend.append({
			"month": ym,
			"short_label": MONTH_ABBR[m],
			"label": f"{MONTH_ABBR[m]} '{str(y)[2:]}",
			"income": inc,
			"expense": exp,
			"surplus": surplus,
		})
	return trend


def _format_category_rows(rows_subset, abbr_suffix=""):
	total = sum(flt(r.amount) for r in rows_subset if flt(r.amount) > 0)
	out = []
	for r in rows_subset:
		amt = flt(r.amount, 2)
		if amt <= 0:
			continue
		raw_label = (r.get("account_name") or r.get("account") or "").strip()
		if abbr_suffix and raw_label.endswith(abbr_suffix):
			raw_label = raw_label[: -len(abbr_suffix)].strip()
		out.append({
			"account": r.account,
			"label": raw_label,
			"amount": amt,
			"pct": round((amt / total) * 100, 1) if total > 0 else 0.0,
		})
	out.sort(key=lambda x: (-x["amount"], x["label"]))
	return out


def _compute_monthly_categories(company, today_str, abbr_suffix=""):
	'''Computes 12-month Income & Expenditure category breakdowns ending in today_str's month.'''
	today_d = getdate(today_str)
	end_year, end_month = today_d.year, today_d.month

	months_12 = []
	for back in range(11, -1, -1):
		total_m = (end_year * 12 + (end_month - 1)) - back
		y, m0 = divmod(total_m, 12)
		months_12.append(f"{y:04d}-{m0 + 1:02d}")

	trend_start = f"{months_12[0]}-01"
	rows = frappe.db.sql(
		'''
		SELECT
			DATE_FORMAT(gle.posting_date, '%%Y-%%m') AS ym,
			acc.root_type,
			gle.account AS account,
			acc.account_name,
			ROUND(
				SUM(
					CASE
						WHEN acc.root_type = 'Income' THEN gle.credit - gle.debit
						ELSE gle.debit - gle.credit
					END
				),
				2
			) AS amount
		FROM `tabGL Entry` gle
		INNER JOIN `tabAccount` acc ON gle.account = acc.name
		WHERE gle.company = %(company)s
			AND gle.is_cancelled = 0
			AND acc.root_type IN ('Income', 'Expense')
			AND gle.posting_date BETWEEN %(trend_start)s AND %(today)s
		GROUP BY DATE_FORMAT(gle.posting_date, '%%Y-%%m'), acc.root_type, gle.account, acc.account_name
		HAVING amount > 0
		ORDER BY ym ASC, acc.root_type ASC, amount DESC, acc.account_name ASC
		''',
		{"company": company, "trend_start": trend_start, "today": today_str},
		as_dict=True,
	)

	grouped = {ym: {"Income": [], "Expense": []} for ym in months_12}
	for r in rows:
		ym = r.get("ym")
		rt = r.get("root_type")
		if ym in grouped and rt in ("Income", "Expense"):
			grouped[ym][rt].append(r)

	monthly_categories = {}
	for ym in months_12:
		monthly_categories[ym] = {
			"income": _format_category_rows(grouped[ym]["Income"], abbr_suffix),
			"expense": _format_category_rows(grouped[ym]["Expense"], abbr_suffix),
		}
	return monthly_categories


def _resolve_month_and_fy(month=None):
	today_str = nowdate()
	target_month = (month or today_str[:7]).strip()
	try:
		anchor = getdate(f"{target_month}-01")
	except Exception:
		anchor = getdate(today_str)
		target_month = today_str[:7]
	month_start = str(get_first_day(anchor))
	month_end = str(get_last_day(anchor))
	fy_start_year = anchor.year if anchor.month >= 4 else anchor.year - 1
	fy_start = f"{fy_start_year}-04-01"
	fy_end = f"{fy_start_year + 1}-03-31"
	return target_month, month_start, month_end, fy_start, fy_end


def _compute_budgets(company, month=None):
	target_month, m_start, m_end, fy_start, fy_end = _resolve_month_and_fy(month)
	budgets = frappe.get_all(
		"Family Budget Limit",
		filters={"company": company, "is_active": 1},
		fields=["name", "budget_name", "expense_account", "period_type", "limit_amount", "is_active"],
		order_by="budget_name asc",
	)
	if not budgets:
		return []

	acc_names = tuple({b.expense_account for b in budgets if b.expense_account})
	acc_labels = {}
	if acc_names:
		for r in frappe.get_all(
			"Account",
			filters={"name": ["in", list(acc_names)]},
			fields=["name", "account_name"],
		):
			acc_labels[r.name] = r.account_name

	today_d = getdate(today())

	def _spent_map(start_d, end_d):
		capped_end = min(getdate(end_d), today_d)
		if getdate(start_d) > capped_end:
			return {}
		rows = frappe.db.sql(
			"""
			SELECT account, COALESCE(SUM(debit - credit), 0) AS spent
			FROM `tabGL Entry`
			WHERE company = %(company)s
				AND is_cancelled = 0
				AND account IN %(accounts)s
				AND posting_date BETWEEN %(s)s AND %(e)s
			GROUP BY account
			""",
			{"company": company, "accounts": acc_names, "s": start_d, "e": str(capped_end)},
			as_dict=True,
		)
		return {r.account: flt(r.spent, 2) for r in rows}

	monthly_spent = _spent_map(m_start, m_end) if acc_names else {}
	yearly_spent = _spent_map(fy_start, fy_end) if acc_names else {}

	out = []
	for b in budgets:
		limit_amt = flt(b.limit_amount, 2)
		spent = yearly_spent.get(b.expense_account, 0.0) if b.period_type == "Yearly" else monthly_spent.get(b.expense_account, 0.0)
		remaining = flt(limit_amt - spent, 2)
		pct = round((spent / limit_amt) * 100, 1) if limit_amt > 0 else 0.0
		if spent > limit_amt:
			status = "over"
		elif pct >= 80.0:
			status = "warning"
		else:
			status = "ok"

		out.append({
			"name": b.name,
			"budget_name": b.budget_name,
			"expense_account": b.expense_account,
			"expense_account_name": acc_labels.get(b.expense_account, b.expense_account),
			"period_type": b.period_type,
			"limit_amount": limit_amt,
			"spent": spent,
			"remaining": remaining,
			"percent_used": pct,
			"status": status,
			"month": target_month,
			"is_active": cint(b.is_active),
		})
	return out


@frappe.whitelist()
def get_accounts_overview():
	"""Returns non-group accounts with live balances (Dr-Cr for Asset, Cr-Dr for Liability/Equity) and pin status."""
	company, can_write = _resolve_portal_company()
	prefs = _get_user_preferences(frappe.session.user, company)
	pinned_set = set(prefs["pinned_accounts"])

	rows = frappe.db.sql(
		f"""
		SELECT
			acc.name,
			acc.account_name,
			acc.account_type,
			acc.root_type,
			acc.parent_account,
			acc.disabled,
			ROUND(COALESCE(SUM(gle.debit - gle.credit), 0), 2) AS raw_dr_cr,
			COUNT(gle.name) AS tx_count,
			MAX(gle.posting_date) AS last_date
		FROM `tabAccount` acc
		LEFT JOIN `tabGL Entry` gle
			ON gle.account = acc.name
			AND gle.company = %(company)s
			AND gle.is_cancelled = 0
			AND gle.posting_date <= %(today)s
		WHERE acc.company = %(company)s
			AND acc.is_group = 0
			AND (
				acc.account_type IN ('Bank', 'Cash', 'Receivable', 'Payable')
				OR {CREDIT_CARD_SQL}
				OR acc.root_type IN ('Asset', 'Liability')
			)
		GROUP BY acc.name, acc.account_name, acc.account_type, acc.root_type, acc.parent_account, acc.disabled
		HAVING acc.disabled = 0 OR ABS(raw_dr_cr) >= 0.01
		ORDER BY acc.root_type ASC, acc.account_type DESC, acc.account_name ASC
		""",
		{"company": company, "today": today()},
		as_dict=True,
	)

	items = []
	for r in rows:
		raw_val = flt(r.raw_dr_cr, 2)
		bal = flt(-raw_val, 2) if r.root_type in ("Liability", "Equity") else raw_val
		# Skip inactive/unused non-liquid zero accounts with 0 transactions
		if not _is_liquid_account(r) and r.tx_count == 0 and abs(bal) < 0.01:
			continue
		items.append({
			"name": r.name,
			"account_name": r.account_name,
			"account_type": r.account_type or "",
			"root_type": r.root_type or "",
			"parent_account": r.parent_account or "",
			"group_label": _classify_account_group(r),
			"balance": bal,
			"tx_count": int(r.tx_count),
			"last_date": str(r.last_date) if r.last_date else None,
			"is_pinned": r.name in pinned_set,
		})

	items.sort(key=lambda x: (0 if x["is_pinned"] else 1, x["group_label"], x["account_name"]))
	return {"items": items, "can_write": can_write, "company": company}


@frappe.whitelist(methods=["POST"])
def adjust_account_balance(account, target_balance, posting_date=None, description="Balance Adjustment"):
	"""Creates and submits a balancing Journal Entry to bring account to target_balance."""
	company, can_write = _resolve_portal_company()
	if not can_write:
		frappe.throw(_("You do not have write permission on this portal."), frappe.PermissionError)

	acc_doc = _validate_account_belongs_to(account, company, "Account")
	target_val = flt(target_balance, 2)
	posting_date = str(getdate(posting_date)) if posting_date else today()
	description = (description or "Balance Adjustment").strip()

	raw_res = frappe.db.sql(
		"""
		SELECT COALESCE(SUM(debit - credit), 0)
		FROM `tabGL Entry`
		WHERE company = %(company)s
			AND account = %(account)s
			AND is_cancelled = 0
			AND posting_date <= %(posting_date)s
		""",
		{"company": company, "account": account, "posting_date": posting_date},
	)
	raw_dr_cr = flt(raw_res[0][0], 2) if raw_res else 0.0
	is_credit_normal = acc_doc.root_type in ("Liability", "Equity", "Income")
	current_balance = flt(-raw_dr_cr, 2) if is_credit_normal else raw_dr_cr
	diff = flt(target_val - current_balance, 2)

	if abs(diff) < 0.01:
		return {
			"adjusted": False,
			"message": "Balance already matches",
			"previous_balance": current_balance,
			"new_balance": current_balance,
		}

	offset_account = _find_adjustment_account(company, account)
	abs_diff = abs(diff)

	# Determine debit/credit direction based on normal balance side
	increase_debit_side = (diff > 0 and not is_credit_normal) or (diff < 0 and is_credit_normal)

	je = frappe.new_doc("Journal Entry")
	je.voucher_type = "Journal Entry"
	je.company = company
	je.posting_date = posting_date
	je.user_remark = f"{description}: {acc_doc.name} ({current_balance} -> {target_val})"
	if increase_debit_side:
		je.append("accounts", {"account": account, "debit_in_account_currency": abs_diff, "credit_in_account_currency": 0})
		je.append("accounts", {"account": offset_account, "debit_in_account_currency": 0, "credit_in_account_currency": abs_diff})
	else:
		je.append("accounts", {"account": offset_account, "debit_in_account_currency": abs_diff, "credit_in_account_currency": 0})
		je.append("accounts", {"account": account, "debit_in_account_currency": 0, "credit_in_account_currency": abs_diff})

	je.flags.ignore_permissions = True
	je.insert()
	je.submit()
	frappe.db.commit()

	return {
		"adjusted": True,
		"journal_entry": je.name,
		"offset_account": offset_account,
		"previous_balance": current_balance,
		"new_balance": target_val,
		"diff": diff,
	}


@frappe.whitelist()
def get_budgets(month=None):
	"""Returns active Family Budget Limit rows with live spent/remaining/status for month or FY."""
	company, can_write = _resolve_portal_company()
	target_month = _resolve_month_and_fy(month)[0]
	return {
		"items": _compute_budgets(company, target_month),
		"month": target_month,
		"can_write": can_write,
	}


@frappe.whitelist(methods=["POST"])
def save_budget(budget_name, expense_account, limit_amount, period_type="Monthly", is_active=1, name=None):
	"""Creates or updates a Family Budget Limit strictly scoped to the user's locked Company."""
	company, can_write = _resolve_portal_company()
	if not can_write:
		frappe.throw(_("You do not have write permission on this portal."), frappe.PermissionError)

	budget_name = (budget_name or "").strip()
	if not budget_name:
		frappe.throw(_("Budget Name is required."))

	limit_val = flt(limit_amount)
	if limit_val <= 0:
		frappe.throw(_("Limit Amount must be greater than zero."))

	period_type = (period_type or "Monthly").strip()
	if period_type not in ("Monthly", "Yearly"):
		frappe.throw(_("Period Type must be Monthly or Yearly."))

	_validate_account_belongs_to(expense_account, company, "Expense Account")

	if name:
		if not frappe.db.exists("Family Budget Limit", name):
			frappe.throw(_("Budget '{0}' does not exist.").format(name))
		doc = frappe.get_doc("Family Budget Limit", name)
		if doc.company != company:
			frappe.throw(_("Budget does not belong to Company '{0}'.").format(company), frappe.PermissionError)
	else:
		doc = frappe.new_doc("Family Budget Limit")
		doc.company = company

	doc.budget_name = budget_name
	doc.expense_account = expense_account
	doc.period_type = period_type
	doc.limit_amount = limit_val
	doc.is_active = 1 if cint(is_active) else 0
	doc.flags.ignore_permissions = True
	doc.save()
	frappe.db.commit()

	return {
		"name": doc.name,
		"company": doc.company,
		"budget_name": doc.budget_name,
		"expense_account": doc.expense_account,
		"period_type": doc.period_type,
		"limit_amount": flt(doc.limit_amount, 2),
		"is_active": cint(doc.is_active),
	}


@frappe.whitelist(methods=["POST"])
def delete_budget(name):
	"""Deletes a Family Budget Limit belonging to the user's locked Company."""
	company, can_write = _resolve_portal_company()
	if not can_write:
		frappe.throw(_("You do not have write permission on this portal."), frappe.PermissionError)

	if not name or not frappe.db.exists("Family Budget Limit", name):
		frappe.throw(_("Budget '{0}' does not exist.").format(name))
	doc_company = frappe.db.get_value("Family Budget Limit", name, "company")
	if doc_company != company:
		frappe.throw(_("Budget does not belong to Company '{0}'.").format(company), frappe.PermissionError)

	frappe.delete_doc("Family Budget Limit", name, ignore_permissions=True)
	frappe.db.commit()
	return {"status": "deleted", "name": name}


@frappe.whitelist()
def get_preferences():
	"""Returns portal UI preferences for (user, company), or defaults if none exists."""
	company, _can_write = _resolve_portal_company()
	return _get_user_preferences(frappe.session.user, company)


@frappe.whitelist(methods=["POST"])
def save_preferences(quick_amounts=None, default_from_account=None, pinned_accounts=None, hidden_cards=None):
	"""Upserts Family Portal Preference for (frappe.session.user, company)."""
	company, _can_write = _resolve_portal_company()
	user = frappe.session.user

	existing_name = frappe.db.get_value(
		"Family Portal Preference",
		{"user": user, "company": company},
		"name",
	)
	if existing_name:
		doc = frappe.get_doc("Family Portal Preference", existing_name)
	else:
		doc = frappe.new_doc("Family Portal Preference")
		doc.user = user
		doc.company = company
		doc.quick_amounts = DEFAULT_QUICK_AMOUNTS

	if quick_amounts is not None:
		clean_nums = []
		for part in str(quick_amounts).split(","):
			val = flt(part.strip())
			if val > 0:
				clean_nums.append(str(int(val) if val == int(val) else val))
		doc.quick_amounts = ",".join(clean_nums) if clean_nums else DEFAULT_QUICK_AMOUNTS

	if default_from_account is not None:
		if default_from_account:
			_validate_account_belongs_to(default_from_account, company, "Default From Account")
			doc.default_from_account = default_from_account
		else:
			doc.default_from_account = None

	if pinned_accounts is not None:
		doc.pinned_accounts = json.dumps(_parse_json_list(pinned_accounts))

	if hidden_cards is not None:
		doc.hidden_cards = json.dumps(_parse_json_list(hidden_cards))

	doc.flags.ignore_permissions = True
	doc.save()
	frappe.db.commit()
	return _get_user_preferences(user, company)


@frappe.whitelist()
def get_automation_rules():
	"""
	Returns all Family Rule Groups (ordered by sort_order ASC, title ASC) with their nested
	Family Automation Rules (ordered by sort_order ASC, title ASC), triggers, and actions.
	"""
	_company, can_write = _resolve_portal_company()

	groups = frappe.get_all(
		"Family Rule Group",
		fields=["name", "title", "sort_order", "is_active", "description"],
		order_by="sort_order asc, title asc",
	)
	rules = frappe.get_all(
		"Family Automation Rule",
		fields=["name", "title", "rule_group", "sort_order", "is_active", "match_all", "stop_processing"],
		order_by="sort_order asc, title asc",
	)

	rule_names = [r.name for r in rules]
	trig_by_rule = {}
	act_by_rule = {}
	if rule_names:
		for t in frappe.get_all(
			"Family Rule Trigger",
			filters={"parenttype": "Family Automation Rule", "parent": ["in", rule_names]},
			fields=["parent", "trigger_type", "trigger_value", "negated"],
			order_by="idx asc",
		):
			trig_by_rule.setdefault(t.parent, []).append(
				{
					"trigger_type": t.trigger_type or "",
					"trigger_value": t.trigger_value or "",
					"negated": cint(t.negated),
				}
			)
		for a in frappe.get_all(
			"Family Rule Action",
			filters={"parenttype": "Family Automation Rule", "parent": ["in", rule_names]},
			fields=["parent", "action_type", "action_value"],
			order_by="idx asc",
		):
			act_by_rule.setdefault(a.parent, []).append(
				{
					"action_type": a.action_type or "",
					"action_value": a.action_value or "",
				}
			)

	rules_by_group = {}
	for r in rules:
		rules_by_group.setdefault(r.rule_group, []).append(
			{
				"name": r.name,
				"title": r.title or "",
				"sort_order": cint(r.sort_order),
				"is_active": cint(r.is_active),
				"match_all": cint(r.match_all),
				"stop_processing": cint(r.stop_processing),
				"triggers": trig_by_rule.get(r.name, []),
				"actions": act_by_rule.get(r.name, []),
			}
		)

	groups_out = []
	for g in groups:
		grules = rules_by_group.get(g.name, [])
		groups_out.append(
			{
				"name": g.name,
				"title": g.title or g.name,
				"sort_order": cint(g.sort_order),
				"is_active": cint(g.is_active),
				"description": g.description or "",
				"active_count": sum(1 for r in grules if r["is_active"] == 1),
				"total_count": len(grules),
				"rules": grules,
			}
		)

	return {
		"groups": groups_out,
		"total_rules": len(rules),
		"active_rules": sum(1 for r in rules if cint(r.is_active) == 1),
		"can_write": can_write,
	}


def _toggle_rule_doc(doctype, docname, is_active):
	_company, can_write = _resolve_portal_company()
	if not can_write:
		frappe.throw(_("You do not have write permission to modify automation rules."), frappe.PermissionError)
	if not docname or not frappe.db.exists(doctype, docname):
		frappe.throw(_("{0} '{1}' does not exist.").format(doctype, docname), frappe.DoesNotExistError)

	doc = frappe.get_doc(doctype, docname)
	doc.is_active = 1 if cint(is_active) else 0
	doc.flags.ignore_permissions = True
	doc.save()
	frappe.db.commit()
	return {"ok": True, "name": doc.name, "is_active": cint(doc.is_active)}


@frappe.whitelist(methods=["POST"])
def toggle_automation_rule(rule_name, is_active):
	"""Enables or disables a Family Automation Rule and clears the rules cache."""
	return _toggle_rule_doc("Family Automation Rule", rule_name, is_active)


@frappe.whitelist(methods=["POST"])
def toggle_rule_group(group_name, is_active):
	"""Enables or disables a Family Rule Group and clears the rules cache."""
	return _toggle_rule_doc("Family Rule Group", group_name, is_active)


@frappe.whitelist()
def test_automation_rule(rule_name, limit=25):
	"""
	Dry-run evaluates a Family Automation Rule against recent submitted Journal Entry records
	(capped at today()) without modifying any ledger entries.
	"""
	company, _can_write = _resolve_portal_company()
	if not rule_name or not frappe.db.exists("Family Automation Rule", rule_name):
		frappe.throw(_("Automation Rule '{0}' does not exist.").format(rule_name), frappe.DoesNotExistError)

	rule_doc = frappe.get_doc("Family Automation Rule", rule_name)
	rule_dict = serialize_rule_doc(rule_doc)

	limit = min(max(cint(limit) or 25, 1), 100)
	je_rows = frappe.db.sql(
		"""
		SELECT je.name, je.posting_date, je.user_remark, je.remark, je._user_tags
		FROM `tabJournal Entry` je
		WHERE je.company = %(company)s
			AND je.docstatus = 1
			AND je.posting_date <= %(today)s
		ORDER BY je.posting_date DESC, je.creation DESC
		LIMIT %(limit)s
		""",
		{"company": company, "today": today(), "limit": limit},
		as_dict=True,
	)
	raw_notes = {r.name: (r.remark or r.user_remark or "") for r in je_rows}
	hydrated = _hydrate_je_list(je_rows)

	matches = []
	for item in hydrated:
		tx_payload = {
			"transaction_type": item.get("tx_type") or "",
			"description": item.get("description") or "",
			"source_account": item.get("from_account") or "",
			"destination_account": item.get("to_account") or "",
			"notes": raw_notes.get(item.get("name"), ""),
			"amount": item.get("amount") or 0.0,
			"tags": item.get("tags") or [],
		}
		res = evaluate_rules(tx_payload, company=company, rules=[rule_dict])
		if res["matched_rules"]:
			acts = res["matched_rules"][0]["actions"]
			matches.append(
				{
					"name": item.get("name"),
					"voucher": item.get("name"),
					"posting_date": item.get("posting_date"),
					"tx_type": item.get("tx_type"),
					"amount": item.get("amount"),
					"source_account": item.get("from_account") or "",
					"destination_account": item.get("to_account") or "",
					"from_account": item.get("from_account") or "",
					"to_account": item.get("to_account") or "",
					"description": item.get("description") or "",
					"preview_actions": acts,
					"previewed_actions": acts,
					"previewed_payload": res["payload"],
					"skipped": res["skipped"],
				}
			)

	return {
		"rule": {
			"name": rule_doc.name,
			"title": rule_doc.title,
			"rule_group": rule_doc.rule_group,
		},
		"tested_count": len(hydrated),
		"matched_count": len(matches),
		"matches": matches,
	}
