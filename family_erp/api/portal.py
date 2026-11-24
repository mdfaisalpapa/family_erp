# Copyright (c) 2026, Mohammed Faisal and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.desk.doctype.tag.tag import DocTags
from frappe.utils import add_days, cint, flt, get_first_day, get_last_day, getdate, nowdate


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
		inferred_type = "Withdrawal"
	elif "Income" in credit_roots:
		inferred_type = "Deposit"
	elif debit_liquid and credit_liquid:
		inferred_type = "Transfer"
	elif credit_liquid and not debit_liquid:
		inferred_type = "Withdrawal"
	elif debit_liquid and not credit_liquid:
		inferred_type = "Deposit"
	else:
		inferred_type = "Transfer"

	return {
		"name": je.name,
		"posting_date": str(je.posting_date),
		"tx_type": inferred_type,
		"amount": amount,
		"from_account": from_account,
		"from_account_name": from_account_name,
		"to_account": to_account,
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

	from_date = str(getdate(from_date)) if from_date else "2022-04-01"
	to_date = str(getdate(to_date)) if to_date else "2027-12-31"

	today_str = nowdate()
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

	# 4. Calendar days (daily income & expense for target month)
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
		{"company": company, "month_start": month_start, "month_end": month_end},
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
		todo_params = {"company": company}
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

	return {
		"summary": summary,
		"accounts": accounts,
		"weekly_expenses": weekly_expenses,
		"calendar_days": calendar_days,
		"top_categories": top_categories,
		"todo_transactions": todo_transactions,
		"tag_totals": tag_totals,
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
	tx_type = (tx_type or "").strip()
	tag = (tag or "").strip()

	conds = ["je.company = %(company)s", "je.docstatus = 1"]
	params = {"company": company}

	if from_date:
		conds.append("je.posting_date >= %(from_date)s")
		params["from_date"] = str(getdate(from_date))
	if to_date:
		conds.append("je.posting_date <= %(to_date)s")
		params["to_date"] = str(getdate(to_date))
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

	tx_type = (tx_type or "").strip()
	if tx_type not in ("Withdrawal", "Deposit", "Transfer", "Family"):
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

	if tx_type == "Withdrawal" and to_acc_doc.root_type == "Expense":
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
def delete_transaction(voucher_no):
	"""
	Cancels a submitted Journal Entry (or its linked Quick Expense / Family Transaction)
	strictly scoped to the user's locked Company. Requires Family Portal User or System Manager.
	"""
	company, can_write = _resolve_portal_company()
	if not can_write:
		frappe.throw(_("You do not have write permission on this portal."), frappe.PermissionError)

	voucher_no = (voucher_no or "").strip()
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
		return {"status": "cancelled", "doctype": "Quick Expense", "name": qe.name, "voucher_no": voucher_no}

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
			"status": "cancelled",
			"doctype": "Family Transaction",
			"name": ft.name,
			"voucher_no": voucher_no,
		}

	je.flags.ignore_permissions = True
	je.cancel()
	frappe.db.commit()
	return {"status": "cancelled", "doctype": "Journal Entry", "name": je.name, "voucher_no": voucher_no}


# --- Templates ---

def _serialize_template(row):
	d = dict(row)
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

	tx_type = (tx_type or "Withdrawal").strip()
	if tx_type not in ("Withdrawal", "Deposit", "Transfer", "Family"):
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
