# Copyright (c) 2026, Mohammed Faisal and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.utils import flt


def execute(filters=None):
	if not filters:
		filters = {}

	company = filters.get("company")
	if not company:
		frappe.throw(_("Company is mandatory"))

	# Validate user permission for the selected company
	if not frappe.has_permission("Company", ptype="read", doc=company):
		frappe.throw(
			_("You do not have permission to view financial statements for Company: {0}").format(company),
			frappe.PermissionError,
		)

	# Find the clearing account for the company
	# If company is one of the 6 family companies, lookup counterparty_clearing_account in Family Accounting Settings
	clearing_account = None
	settings = frappe.get_single("Family Accounting Settings")

	for mapping in settings.company_mappings:
		if mapping.family_company == company:
			clearing_account = mapping.counterparty_clearing_account
			break

	if not clearing_account:
		# Fallback: check if standard Due to Faisal Household exists in that company
		candidate = frappe.db.get_value(
			"Account",
			{"company": company, "account_name": "Due to Faisal Household"},
			"name",
		)
		if candidate:
			clearing_account = candidate
		else:
			frappe.throw(
				_("No clearing account found for Company {0} in Family Accounting Settings").format(company)
			)

	columns = get_columns()
	data = get_data(filters, clearing_account)

	return columns, data


def get_columns():
	return [
		{
			"label": _("Posting Date"),
			"fieldname": "posting_date",
			"fieldtype": "Date",
			"width": 110,
		},
		{
			"label": _("Voucher Type"),
			"fieldname": "voucher_type",
			"fieldtype": "Link",
			"options": "DocType",
			"width": 130,
		},
		{
			"label": _("Voucher No"),
			"fieldname": "voucher_no",
			"fieldtype": "Dynamic Link",
			"options": "voucher_type",
			"width": 180,
		},
		{
			"label": _("Description"),
			"fieldname": "description",
			"fieldtype": "Data",
			"width": 300,
		},
		{
			"label": _("Settled / Paid (Dr)"),
			"fieldname": "debit",
			"fieldtype": "Currency",
			"width": 150,
		},
		{
			"label": _("Billed / Due (Cr)"),
			"fieldname": "credit",
			"fieldtype": "Currency",
			"width": 150,
		},
		{
			"label": _("Net Due to Faisal (Balance)"),
			"fieldname": "balance",
			"fieldtype": "Currency",
			"width": 200,
		},
	]


def get_data(filters, clearing_account):
	company = filters.get("company")
	from_date = filters.get("from_date")
	to_date = filters.get("to_date")

	data = []

	# Calculate opening balance if from_date is specified
	opening_balance = 0.0
	if from_date:
		opening_res = frappe.db.sql(
			"""
			SELECT SUM(credit - debit)
			FROM `tabGL Entry`
			WHERE is_cancelled = 0
				AND company = %(company)s
				AND account = %(account)s
				AND posting_date < %(from_date)s
			""",
			{"company": company, "account": clearing_account, "from_date": from_date},
		)
		opening_balance = flt(opening_res[0][0]) if opening_res and opening_res[0][0] else 0.0

		data.append(
			{
				"posting_date": from_date,
				"voucher_type": "",
				"voucher_no": "",
				"description": _("Opening Balance"),
				"debit": 0.0,
				"credit": 0.0,
				"balance": opening_balance,
			}
		)

	# Query GL Entries within date range
	conditions = ["gle.is_cancelled = 0", "gle.company = %(company)s", "gle.account = %(account)s"]
	values = {"company": company, "account": clearing_account}

	if from_date:
		conditions.append("gle.posting_date >= %(from_date)s")
		values["from_date"] = from_date
	if to_date:
		conditions.append("gle.posting_date <= %(to_date)s")
		values["to_date"] = to_date

	where_clause = " AND ".join(conditions)

	entries = frappe.db.sql(
		f"""
		SELECT
			gle.posting_date,
			gle.voucher_type,
			gle.voucher_no,
			gle.debit,
			gle.credit,
			gle.remarks,
			je.user_remark
		FROM `tabGL Entry` gle
		LEFT JOIN `tabJournal Entry` je ON gle.voucher_no = je.name
		WHERE {where_clause}
		ORDER BY gle.posting_date ASC, gle.creation ASC
		""",
		values,
		as_dict=True,
	)

	running_balance = opening_balance
	total_debit = 0.0
	total_credit = 0.0

	for entry in entries:
		dr = flt(entry.debit)
		cr = flt(entry.credit)
		# Net Due to Faisal: credit - debit (positive = Amount You Owe Faisal, negative = Credit/Advance)
		running_balance += cr - dr
		total_debit += dr
		total_credit += cr

		desc = entry.user_remark or entry.remarks or ""

		data.append(
			{
				"posting_date": entry.posting_date,
				"voucher_type": entry.voucher_type,
				"voucher_no": entry.voucher_no,
				"description": desc,
				"debit": dr,
				"credit": cr,
				"balance": running_balance,
			}
		)

	# Summary / total row
	data.append(
		{
			"posting_date": None,
			"voucher_type": "",
			"voucher_no": "",
			"description": _("Total"),
			"debit": total_debit,
			"credit": total_credit,
			"balance": running_balance,
		}
	)

	return data
