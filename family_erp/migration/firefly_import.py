import json
import os
import re
import frappe
from frappe import _
from frappe.utils import flt, getdate
from family_erp.setup.install import after_install

# Map of 6 Family Members to Company Details and Firefly Account Name
FAMILY_COMPANIES = {
	"SB": {
		"company_name": "Saira Banu",
		"abbr": "SB",
		"firefly_account_name": "Receivable  : Saira Banu",
	},
	"FB": {
		"company_name": "Farida Begum",
		"abbr": "FB",
		"firefly_account_name": "Receivable  : Farida Begum",
	},
	"IMS": {
		"company_name": "Ikram Mohammed Shah",
		"abbr": "IMS",
		"firefly_account_name": "Receivable  : Ikram Mohammed Shah",
	},
	"SAS": {
		"company_name": "Saad Adil Shah",
		"abbr": "SAS",
		"firefly_account_name": "Receivable  : Saad Adil Shah",
	},
	"SS": {
		"company_name": "Sakina Sadiqa",
		"abbr": "SS",
		"firefly_account_name": "Receivable  : Sakina Sadiqa",
	},
	"SF": {
		"company_name": "Shahi Firdous",
		"abbr": "SF",
		"firefly_account_name": "Receivable  : Shahi Firdous",
	},
}

DEFAULT_JSON_PATH = os.path.abspath(
	os.path.join(os.path.dirname(__file__), "..", "..", "data", "firefly_export.json")
)


def get_account_name(account_name, company):
	abbr = frappe.get_cached_value("Company", company, "abbr")
	return f"{account_name} - {abbr}"


def get_default_cost_center(company):
	cc = frappe.db.get_value("Company", company, "cost_center")
	if not cc:
		cc = frappe.db.get_value("Cost Center", {"company": company, "is_group": 0}, "name")
	return cc


def get_or_create_account(
	account_name, company, parent_account, root_type=None, account_type=None, is_group=0
):
	full_name = get_account_name(account_name, company)
	if frappe.db.exists("Account", full_name):
		return full_name

	parent_full_name = None
	if parent_account:
		if f" - " in parent_account:
			parent_full_name = parent_account
		else:
			parent_full_name = get_account_name(parent_account, company)

	account = frappe.new_doc("Account")
	account.account_name = account_name
	account.company = company
	account.parent_account = parent_full_name
	account.is_group = is_group
	if root_type:
		account.root_type = root_type
	if account_type:
		account.account_type = account_type

	account.flags.ignore_permissions = True
	account.flags.ignore_mandatory = True
	account.insert()
	return account.name


def bootstrap_companies():
	"""Create Fiscal Years, Companies, Accounts, Parent Groups, and Family Accounting Settings."""
	print(">>> Step 1: Bootstrapping Fiscal Years...")
	fiscal_years = [
		("2022-2023", "2022-04-01", "2023-03-31"),
		("2023-2024", "2023-04-01", "2024-03-31"),
		("2024-2025", "2024-04-01", "2025-03-31"),
		("2025-2026", "2025-04-01", "2026-03-31"),
		("2026-2027", "2026-04-01", "2027-03-31"),
		("2027-2028", "2027-04-01", "2028-03-31"),
	]
	for fy, start, end in fiscal_years:
		if not frappe.db.exists("Fiscal Year", fy):
			doc = frappe.new_doc("Fiscal Year")
			doc.year = fy
			doc.year_start_date = start
			doc.year_end_date = end
			doc.flags.ignore_permissions = True
			doc.insert()
			print(f"Created Fiscal Year: {fy}")

	# Ensure Custom Field on Journal Entry
	after_install()

	print(">>> Step 2: Bootstrapping Companies...")
	all_companies = [
		{"company_name": "Faisal Household", "abbr": "FH"},
		{"company_name": "Saira Banu", "abbr": "SB"},
		{"company_name": "Farida Begum", "abbr": "FB"},
		{"company_name": "Ikram Mohammed Shah", "abbr": "IMS"},
		{"company_name": "Saad Adil Shah", "abbr": "SAS"},
		{"company_name": "Sakina Sadiqa", "abbr": "SS"},
		{"company_name": "Shahi Firdous", "abbr": "SF"},
	]

	for c in all_companies:
		c_name = c["company_name"]
		if not frappe.db.exists("Company", c_name):
			comp = frappe.new_doc("Company")
			comp.company_name = c_name
			comp.abbr = c["abbr"]
			comp.default_currency = "INR"
			comp.country = "India"
			comp.create_chart_of_accounts_based_on = "Standard Template"
			comp.chart_of_accounts = "Standard"
			comp.flags.ignore_permissions = True
			comp.insert()
			print(f"Created Company: {c_name} ({c['abbr']})")

	frappe.db.commit()
	primary_company = "Faisal Household"

	print(">>> Step 3: Setting up Parent Groups in Faisal Household...")
	# Parent groups under proper root types
	get_or_create_account(
		"Wallets & Vouchers", primary_company, "Bank Accounts", root_type="Asset", is_group=1
	)
	get_or_create_account(
		"Family Receivables", primary_company, "Current Assets", root_type="Asset", is_group=1
	)
	get_or_create_account(
		"Credit Cards", primary_company, "Current Liabilities", root_type="Liability", is_group=1
	)
	get_or_create_account(
		"Loans & Mortgages", primary_company, "Non-Current Liabilities", root_type="Liability", is_group=1
	)

	# Equity Accounts in Faisal Household
	get_or_create_account(
		"Opening Net Worth", primary_company, "Equity", root_type="Equity"
	)
	get_or_create_account(
		"Reconciliation Adjustments", primary_company, "Equity", root_type="Equity"
	)
	get_or_create_account(
		"Retained Earnings", primary_company, "Equity", root_type="Equity"
	)

	# Ensure clearing accounts in Faisal Household under Family Receivables
	for key, f_info in FAMILY_COMPANIES.items():
		fam_name = f_info["company_name"]
		get_or_create_account(
			f"Due from {fam_name}",
			primary_company,
			"Family Receivables",
			root_type="Asset",
		)

	print(">>> Step 4: Setting up Standardized Accounts in 6 Family Companies...")
	for key, f_info in FAMILY_COMPANIES.items():
		fam_company = f_info["company_name"]
		get_or_create_account(
			"Due to Faisal Household",
			fam_company,
			"Current Liabilities",
			root_type="Liability",
		)
		get_or_create_account(
			"Personal Bank / Cash",
			fam_company,
			"Bank Accounts",
			root_type="Asset",
			account_type="Bank",
		)
		get_or_create_account(
			"Temporary Opening",
			fam_company,
			"Temporary Accounts",
			root_type="Asset",
		)
		get_or_create_account(
			"Property & Municipal Tax",
			fam_company,
			"Indirect Expenses",
			root_type="Expense",
			account_type="Expense Account",
		)
		get_or_create_account(
			"Water & Utility Bills",
			fam_company,
			"Indirect Expenses",
			root_type="Expense",
			account_type="Expense Account",
		)
		get_or_create_account(
			"Electricity (EB) Charges",
			fam_company,
			"Indirect Expenses",
			root_type="Expense",
			account_type="Expense Account",
		)
		get_or_create_account(
			"General Expenses",
			fam_company,
			"Indirect Expenses",
			root_type="Expense",
			account_type="Expense Account",
		)

	print(">>> Step 5: Configuring Family Accounting Settings...")
	settings = frappe.get_single("Family Accounting Settings")
	settings.primary_company = primary_company
	settings.company_mappings = []

	for key, f_info in FAMILY_COMPANIES.items():
		fam_company = f_info["company_name"]
		primary_clearing = get_account_name(f"Due from {fam_company}", primary_company)
		counterparty_clearing = get_account_name("Due to Faisal Household", fam_company)
		default_expense = get_account_name("General Expenses", fam_company)
		default_settlement = get_account_name("Personal Bank / Cash", fam_company)

		settings.append(
			"company_mappings",
			{
				"family_company": fam_company,
				"primary_clearing_account": primary_clearing,
				"counterparty_clearing_account": counterparty_clearing,
				"default_expense_account": default_expense,
				"default_settlement_account": default_settlement,
				"firefly_account_name": f_info["firefly_account_name"],
			},
		)

	settings.flags.ignore_permissions = True
	settings.save()
	frappe.db.commit()
	print("Family Accounting Settings saved successfully.")


def clean_pl_account_name(name):
	"""Sanitize P&L account names so they never collide with reserved group accounts."""
	n = (name or "").strip()
	if n.lower() in ("investments", "investment"):
		return "Investment Income / Expense"
	return n


def get_target_account_for_firefly_leg(leg, category_name, primary_company):
	"""Resolve the exact ERPNext account for an individual transaction leg in Faisal Household."""
	acc_name = leg.get("account_name", "").strip()
	acc_type = leg.get("account_type", "")

	# 1. Family Receivables
	for key, f_info in FAMILY_COMPANIES.items():
		if acc_name == f_info["firefly_account_name"]:
			return get_account_name(f"Due from {f_info['company_name']}", primary_company)

	# Other special receivables / assets
	if acc_name.startswith("Receivable") or "LTC Claim" in acc_name or "Recievable" in acc_name:
		clean_name = acc_name.replace("Receivable  :", "Receivable -").replace(
			"Receivable :", "Receivable -"
		)
		if "LTC Claim" in acc_name:
			clean_name = "LTC Claim Receivable"
		return get_or_create_account(
			clean_name, primary_company, "Family Receivables", root_type="Asset"
		)

	if acc_name in ("Ahmed Sayeed", "Salary Clearing", "Cancelled Transactions"):
		return get_or_create_account(
			acc_name, primary_company, "Current Assets", root_type="Asset"
		)

	# 2. Credit Cards
	if acc_name.startswith("Credit Card"):
		clean_name = acc_name.replace("Credit Card :", "Credit Card -").replace(
			"Credit Card:", "Credit Card -"
		)
		return get_or_create_account(
			clean_name, primary_company, "Credit Cards", root_type="Liability"
		)

	# 3. Fixed Assets / Flats
	if acc_name.startswith("Flat :"):
		clean_name = acc_name.replace("Flat :", "Property -")
		return get_or_create_account(
			clean_name, primary_company, "Fixed Assets", root_type="Asset"
		)

	# 4. Loans & Mortgages
	if (
		acc_name.startswith("Loan :")
		or "Mortgage" in acc_type
		or "Debt" in acc_type
		or "Security Deposit Advance From Tenants" in acc_name
		or acc_name.startswith("Liability credit")
	):
		clean_name = acc_name.replace("Loan :", "Loan -")
		return get_or_create_account(
			clean_name, primary_company, "Loans & Mortgages", root_type="Liability"
		)

	# 5. Bank Accounts
	if acc_name.startswith("Bank :") or acc_name == "SBI Faisal savings account":
		clean_name = acc_name.replace("Bank :", "Bank -")
		return get_or_create_account(
			clean_name, primary_company, "Bank Accounts", root_type="Asset", account_type="Bank"
		)

	# 6. Cash In Hand
	if acc_name.startswith("Cash") or acc_type == "Cash account":
		clean_name = acc_name.replace("Cash :", "Cash -")
		return get_or_create_account(
			clean_name, primary_company, "Cash In Hand", root_type="Asset", account_type="Cash"
		)

	# 7. Wallets & Vouchers
	if (
		acc_name.startswith("Amazon")
		or acc_name.startswith("Wallet :")
		or "Gyftr" in acc_name
		or "Voucher" in acc_name
		or acc_name.startswith("Big Basket")
		or acc_name.startswith("Flipkart")
		or acc_name.startswith("Swiggy")
		or acc_name.startswith("Zomato")
		or acc_name.startswith("Myntra")
		or "Reward Points" in acc_name
	):
		clean_name = (
			acc_name.replace("Wallet :", "Wallet -")
			.replace("Amazon Pay :", "Amazon Pay -")
			.replace("Amazon Voucher :", "Amazon Voucher -")
			.replace("Reward Points :  ", "Reward Points - ")
		)
		return get_or_create_account(
			clean_name, primary_company, "Wallets & Vouchers", root_type="Asset", account_type="Bank"
		)

	# 8. Investments
	if acc_name.startswith("Investments :"):
		clean_name = acc_name.replace("Investments :", "Investment -")
		return get_or_create_account(
			clean_name, primary_company, "Investments", root_type="Asset"
		)

	# 9. Opening Balance / Reconciliation
	if (
		acc_type in ("Initial balance account", "Opening balance account")
		or "Initial balance" in acc_name
	):
		return get_or_create_account(
			"Opening Net Worth",
			primary_company,
			"Equity",
			root_type="Equity",
		)
	if acc_type == "Reconciliation account" or "Reconciliation" in acc_name or "reconciliation" in acc_name:
		return get_or_create_account(
			"Reconciliation Adjustments",
			primary_company,
			"Equity",
			root_type="Equity",
		)

	# 10. Expense or Revenue Accounts (P&L heads)
	if acc_type == "Expense account":
		cat_name = clean_pl_account_name(category_name or acc_name or "General Expenses")
		return get_or_create_account(
			cat_name[:100], primary_company, "Indirect Expenses", root_type="Expense"
		)
	if acc_type == "Revenue account":
		cat_name = clean_pl_account_name(category_name or acc_name or "Indirect Income")
		return get_or_create_account(
			cat_name[:100], primary_company, "Indirect Income", root_type="Income"
		)

	# Fallback
	if leg.get("amount", 0) > 0:
		cat_name = clean_pl_account_name(category_name or acc_name or "General Expenses")
		return get_or_create_account(
			cat_name[:100], primary_company, "Indirect Expenses", root_type="Expense"
		)
	else:
		cat_name = clean_pl_account_name(category_name or acc_name or "Indirect Income")
		return get_or_create_account(
			cat_name[:100], primary_company, "Indirect Income", root_type="Income"
		)


def route_counterparty_account_by_keywords(description, category, fam_company):
	"""Smart keyword routing for family expenses."""
	text = f"{description or ''} {category or ''}".lower()
	if any(k in text for k in ("tax", "municipal", "corporation", "property tax")):
		head = "Property & Municipal Tax"
	elif any(k in text for k in ("water", "utility", "sewerage", "metro water")):
		head = "Water & Utility Bills"
	elif any(k in text for k in ("eb", "electricity", "tneb", "power")):
		head = "Electricity (EB) Charges"
	else:
		head = "General Expenses"
	return get_account_name(head, fam_company)


def import_journals(json_path=None, dry_run=False, limit=None):
	"""Import Firefly journals into Family Transactions and Journal Entries."""
	json_file = json_path or DEFAULT_JSON_PATH
	if not os.path.exists(json_file):
		frappe.throw(f"Firefly export JSON not found at: {json_file}")

	with open(json_file, "r") as f:
		data = json.load(f)

	journals = data.get("journals", [])
	print(f"Loaded {len(journals)} total journals from {json_file}")

	settings = frappe.get_single("Family Accounting Settings")
	primary_company = settings.primary_company or "Faisal Household"
	primary_cost_center = get_default_cost_center(primary_company)

	# Build lookup of Firefly account name to Family Company Mapping
	mapping_by_firefly_name = {}
	for m in settings.company_mappings:
		if m.firefly_account_name:
			mapping_by_firefly_name[m.firefly_account_name] = m

	# Pre-load existing firefly_journal_id sets to avoid querying DB per journal
	existing_ft_ids = set(
		frappe.db.sql_list(
			"SELECT firefly_journal_id FROM `tabFamily Transaction` WHERE firefly_journal_id IS NOT NULL"
		)
	)
	existing_je_ids = set(
		frappe.db.sql_list(
			"SELECT firefly_journal_id FROM `tabJournal Entry` WHERE firefly_journal_id IS NOT NULL"
		)
	)

	family_ft_count = 0
	standard_je_count = 0
	mirrored_je_count = 0
	skipped_count = 0
	processed_new = 0

	for idx, j in enumerate(journals, 1):
		j_id = str(j.get("id"))
		posting_date = j.get("date")
		j_type = j.get("type", "")
		desc = j.get("description", "") or ""
		cat = j.get("category", "") or ""
		tags = j.get("tags", "") or ""
		legs = j.get("legs", [])

		# Idempotency check via pre-loaded sets
		if j_id in existing_ft_ids or j_id in existing_je_ids:
			skipped_count += 1
			continue

		# Skip zero-amount reconciliation journals
		if all(flt(l.get("amount", 0)) == 0 for l in legs):
			skipped_count += 1
			continue

		if limit is not None and processed_new >= int(limit):
			break

		# Check if this qualifies for Family Transaction:
		# 2 legs, Transfer type, one leg is a mapped Family Receivable account, other leg is regular Asset/Liability
		is_family_tx = False
		fam_leg = None
		funding_leg = None

		if j_type == "Transfer" and len(legs) == 2:
			for leg in legs:
				if leg.get("account_name") in mapping_by_firefly_name:
					fam_leg = leg
				else:
					funding_leg = leg

			if fam_leg and funding_leg:
				f_type = funding_leg.get("account_type", "")
				if f_type not in (
					"Initial balance account",
					"Opening balance account",
					"Reconciliation account",
				):
					is_family_tx = True

		if is_family_tx:
			# Process as Family Transaction
			mapping = mapping_by_firefly_name[fam_leg["account_name"]]
			fam_company = mapping.family_company
			amount = abs(flt(fam_leg["amount"]))

			# Resolve funding account in Faisal Household
			funding_acc = get_target_account_for_firefly_leg(
				funding_leg, cat, primary_company
			)

			if flt(fam_leg["amount"]) > 0:
				tx_type = "Expense Paid on Behalf"
				counterparty_acc = route_counterparty_account_by_keywords(
					desc, cat, fam_company
				)
			else:
				tx_type = "Settlement Received"
				counterparty_acc = (
					mapping.default_settlement_account
					or get_account_name("Personal Bank / Cash", fam_company)
				)

			if not dry_run:
				ft = frappe.new_doc("Family Transaction")
				ft.posting_date = posting_date
				ft.transaction_type = tx_type
				ft.primary_company = primary_company
				ft.funding_account = funding_acc
				ft.family_company = fam_company
				ft.counterparty_account = counterparty_acc
				ft.amount = amount
				ft.description = f"[Firefly #{j_id}] {desc} | Cat: {cat} | Tags: {tags}".strip()
				ft.firefly_journal_id = j_id
				ft.flags.ignore_permissions = True
				ft.insert()
				ft.submit()

			existing_ft_ids.add(j_id)
			family_ft_count += 1
		else:
			# Process as standard Journal Entry in Faisal Household
			is_opening = j_type == "Opening balance"
			voucher_type = "Opening Entry" if is_opening else "Journal Entry"
			remark = f"[Firefly #{j_id}] {desc} | Cat: {cat} | Tags: {tags}".strip()

			je_accounts = []
			family_legs_found = []
			for leg in legs:
				leg_amount = flt(leg.get("amount", 0))
				target_acc = get_target_account_for_firefly_leg(
					leg, cat, primary_company
				)
				debit = leg_amount if leg_amount > 0 else 0
				credit = abs(leg_amount) if leg_amount < 0 else 0

				row = {
					"account": target_acc,
					"debit_in_account_currency": debit,
					"credit_in_account_currency": credit,
				}
				root_t = frappe.db.get_value("Account", target_acc, "root_type")
				if root_t in ("Expense", "Income"):
					row["cost_center"] = primary_cost_center

				je_accounts.append(row)

				if leg.get("account_name") in mapping_by_firefly_name:
					family_legs_found.append(leg)

			# Ensure balanced entry
			tot_dr = sum(a["debit_in_account_currency"] for a in je_accounts)
			tot_cr = sum(a["credit_in_account_currency"] for a in je_accounts)
			diff = round(tot_dr - tot_cr, 2)
			if diff != 0:
				round_off = get_or_create_account(
					"Round Off", primary_company, "Indirect Expenses", root_type="Expense"
				)
				if diff > 0:
					je_accounts.append(
						{
							"account": round_off,
							"debit_in_account_currency": 0,
							"credit_in_account_currency": diff,
							"cost_center": primary_cost_center,
						}
					)
				else:
					je_accounts.append(
						{
							"account": round_off,
							"debit_in_account_currency": abs(diff),
							"credit_in_account_currency": 0,
							"cost_center": primary_cost_center,
						}
					)

			if not dry_run:
				je = frappe.new_doc("Journal Entry")
				je.voucher_type = voucher_type
				if is_opening:
					je.is_opening = "Yes"
				je.company = primary_company
				je.posting_date = posting_date
				je.user_remark = remark
				je.firefly_journal_id = j_id

				for acc_row in je_accounts:
					je.append("accounts", acc_row)

				je.flags.ignore_permissions = True
				je.insert()
				je.submit()

			existing_je_ids.add(j_id)
			standard_je_count += 1

			# Mirrored Journal Entry for non-Transfer journals touching a Family Receivable account
			# Also handles multi-family legs (e.g. transfer between two family members)
			for fam_leg_item in family_legs_found:
				fam_mapping = mapping_by_firefly_name[fam_leg_item["account_name"]]
				fam_co = fam_mapping.family_company
				fam_abbr = frappe.get_cached_value("Company", fam_co, "abbr")
				mirror_jid = f"{j_id}-mirror-{fam_abbr}"

				if mirror_jid not in existing_je_ids:
					fam_amount = flt(fam_leg_item.get("amount", 0))
					abs_fam_amount = abs(fam_amount)
					mirror_due_to = get_account_name("Due to Faisal Household", fam_co)
					fam_cost_center = get_default_cost_center(fam_co)

					if is_opening:
						mirror_voucher_type = "Opening Entry"
						mirror_other_acc = get_account_name("Temporary Opening", fam_co)
					else:
						mirror_voucher_type = "Journal Entry"
						mirror_other_acc = get_account_name("Personal Bank / Cash", fam_co)

					mirror_accounts = []
					if fam_amount > 0:
						mirror_accounts.append({
							"account": mirror_other_acc,
							"debit_in_account_currency": abs_fam_amount,
							"credit_in_account_currency": 0,
						})
						mirror_accounts.append({
							"account": mirror_due_to,
							"debit_in_account_currency": 0,
							"credit_in_account_currency": abs_fam_amount,
						})
					else:
						mirror_accounts.append({
							"account": mirror_due_to,
							"debit_in_account_currency": abs_fam_amount,
							"credit_in_account_currency": 0,
						})
						mirror_accounts.append({
							"account": mirror_other_acc,
							"debit_in_account_currency": 0,
							"credit_in_account_currency": abs_fam_amount,
						})

					for ma in mirror_accounts:
						root_t = frappe.db.get_value("Account", ma["account"], "root_type")
						if root_t in ("Expense", "Income"):
							ma["cost_center"] = fam_cost_center

					if not dry_run:
						mirror_je = frappe.new_doc("Journal Entry")
						mirror_je.voucher_type = mirror_voucher_type
						if is_opening:
							mirror_je.is_opening = "Yes"
						mirror_je.company = fam_co
						mirror_je.posting_date = posting_date
						mirror_je.user_remark = f"[Mirrored from Firefly #{j_id}] {desc}".strip()
						mirror_je.firefly_journal_id = mirror_jid

						for acc_row in mirror_accounts:
							mirror_je.append("accounts", acc_row)

						mirror_je.flags.ignore_permissions = True
						mirror_je.insert()
						mirror_je.submit()

					existing_je_ids.add(mirror_jid)
					mirrored_je_count += 1

		processed_new += 1
		if processed_new % 50 == 0:
			if not dry_run:
				frappe.db.commit()
			print(f"Processed {processed_new} new journals ({idx}/{len(journals)} total checked)...")

	if not dry_run:
		frappe.db.commit()

	print(
		f">>> Import Batch Summary: {family_ft_count} Family Transactions, {standard_je_count} Journal Entries ({mirrored_je_count} Mirrored), {skipped_count} Skipped."
	)


def reconcile(json_path=None):
	"""Compare Firefly net balances against ERPNext ledger balances."""
	from erpnext.accounts.utils import get_balance_on

	json_file = json_path or DEFAULT_JSON_PATH
	if not os.path.exists(json_file):
		frappe.throw(f"Firefly export JSON not found at: {json_file}")

	with open(json_file, "r") as f:
		data = json.load(f)

	settings = frappe.get_single("Family Accounting Settings")
	primary_company = settings.primary_company or "Faisal Household"

	print("\n" + "=" * 95)
	print(f"RECONCILIATION SUMMARY: FIREFLY III vs. FAISAL HOUSEHOLD (as of 2027-12-31)")
	print("=" * 95)
	print(
		f"{'Firefly Account':<35} | {'ERPNext Account':<35} | {'Firefly':<12} | {'ERPNext':<12} | {'Diff'}"
	)
	print("-" * 95)

	matches = 0
	mismatches = 0

	for acc in data.get("accounts", []):
		acc_type = acc.get("account_type", "")
		if acc_type not in (
			"Asset account",
			"Cash account",
			"Liability credit account",
			"Debt",
			"Mortgage",
		):
			continue

		firefly_bal = flt(acc.get("net_balance", 0))
		target_acc = get_target_account_for_firefly_leg(
			{"account_name": acc["name"], "account_type": acc_type, "amount": 0},
			"",
			primary_company,
		)

		erp_bal = flt(get_balance_on(target_acc, date="2027-12-31", company=primary_company))
		diff = round(firefly_bal - erp_bal, 2)

		if abs(diff) < 0.05:
			matches += 1
		else:
			mismatches += 1
			print(
				f"{acc['name'][:35]:<35} | {target_acc[:35]:<35} | {firefly_bal:12.2f} | {erp_bal:12.2f} | {diff:8.2f}"
			)

	print("-" * 95)
	print(f"Account Balance Status: {matches} Matched, {mismatches} Mismatched.")

	print("\n" + "=" * 95)
	print(f"INTER-COMPANY CLEARING BALANCE VERIFICATION")
	print("=" * 95)
	print(
		f"{'Family Company':<25} | {'Due from (FH)':<15} | {'Due to (Fam Co)':<15} | {'Status'}"
	)
	print("-" * 95)

	for m in settings.company_mappings:
		fam_co = m.family_company
		due_from = flt(
			get_balance_on(m.primary_clearing_account, date="2027-12-31", company=primary_company)
		)
		due_to = flt(
			get_balance_on(m.counterparty_clearing_account, date="2027-12-31", company=fam_co)
		)
		is_mirrored = abs(round(due_from + due_to, 2)) < 0.05
		status = "BALANCED" if is_mirrored else f"UNBALANCED (Diff: {round(due_from + due_to, 2)})"
		print(f"{fam_co:<25} | {due_from:15.2f} | {due_to:15.2f} | {status}")
	print("=" * 95 + "\n")


def sync_missing_mirrors(json_path=None):
	"""Scan non-Transfer and multi-family journals and ensure mirror JVs exist in family companies."""
	json_file = json_path or DEFAULT_JSON_PATH
	if not os.path.exists(json_file):
		frappe.throw(f"Firefly export JSON not found at: {json_file}")

	with open(json_file, "r") as f:
		export_data = json.load(f)

	journals = export_data.get("journals", [])
	settings = frappe.get_single("Family Accounting Settings")
	primary_company = settings.primary_company
	primary_cost_center = get_default_cost_center(primary_company)

	mapping_by_firefly_name = {}
	for m in settings.company_mappings:
		if m.firefly_account_name:
			mapping_by_firefly_name[m.firefly_account_name] = m

	existing_je_ids = set(
		frappe.db.sql_list(
			"SELECT firefly_journal_id FROM `tabJournal Entry` WHERE firefly_journal_id IS NOT NULL"
		)
	)

	synced_count = 0
	for j in journals:
		j_id = str(j.get("id"))
		posting_date = j.get("date")
		j_type = j.get("type", "")
		desc = j.get("description", "") or ""
		legs = j.get("legs", [])

		family_legs = [l for l in legs if l.get("account_name") in mapping_by_firefly_name]
		if not family_legs:
			continue

		# If it was imported as Family Transaction, both legs are already handled by FT engine
		ft_exists = frappe.db.exists("Family Transaction", {"firefly_journal_id": j_id})
		if ft_exists:
			continue

		is_opening = j_type == "Opening balance"

		for fam_leg_item in family_legs:
			fam_mapping = mapping_by_firefly_name[fam_leg_item["account_name"]]
			fam_co = fam_mapping.family_company
			fam_abbr = frappe.get_cached_value("Company", fam_co, "abbr")
			mirror_jid = f"{j_id}-mirror-{fam_abbr}"

			if mirror_jid not in existing_je_ids:
				fam_amount = flt(fam_leg_item.get("amount", 0))
				abs_fam_amount = abs(fam_amount)
				mirror_due_to = get_account_name("Due to Faisal Household", fam_co)
				fam_cost_center = get_default_cost_center(fam_co)

				if is_opening:
					mirror_voucher_type = "Opening Entry"
					mirror_other_acc = get_account_name("Temporary Opening", fam_co)
				else:
					mirror_voucher_type = "Journal Entry"
					mirror_other_acc = get_account_name("Personal Bank / Cash", fam_co)

				mirror_accounts = []
				if fam_amount > 0:
					mirror_accounts.append({
						"account": mirror_other_acc,
						"debit_in_account_currency": abs_fam_amount,
						"credit_in_account_currency": 0,
					})
					mirror_accounts.append({
						"account": mirror_due_to,
						"debit_in_account_currency": 0,
						"credit_in_account_currency": abs_fam_amount,
					})
				else:
					mirror_accounts.append({
						"account": mirror_due_to,
						"debit_in_account_currency": abs_fam_amount,
						"credit_in_account_currency": 0,
					})
					mirror_accounts.append({
						"account": mirror_other_acc,
						"debit_in_account_currency": 0,
						"credit_in_account_currency": abs_fam_amount,
					})

				for ma in mirror_accounts:
					root_t = frappe.db.get_value("Account", ma["account"], "root_type")
					if root_t in ("Expense", "Income"):
						ma["cost_center"] = fam_cost_center

				mirror_je = frappe.new_doc("Journal Entry")
				mirror_je.voucher_type = mirror_voucher_type
				if is_opening:
					mirror_je.is_opening = "Yes"
				mirror_je.company = fam_co
				mirror_je.posting_date = posting_date
				mirror_je.user_remark = f"[Mirrored from Firefly #{j_id}] {desc}".strip()
				mirror_je.firefly_journal_id = mirror_jid

				for acc_row in mirror_accounts:
					mirror_je.append("accounts", acc_row)

				mirror_je.flags.ignore_permissions = True
				mirror_je.insert()
				mirror_je.submit()

				existing_je_ids.add(mirror_jid)
				synced_count += 1
				print(f"Created missing mirror: {mirror_jid} -> {mirror_je.name}")

	if synced_count > 0:
		frappe.db.commit()
	print(f"Total missing mirrors synced: {synced_count}")


def run(json_path=None, dry_run=False):
	bootstrap_companies()
	import_journals(json_path=json_path, dry_run=dry_run)
	sync_missing_mirrors(json_path=json_path)
	reconcile(json_path=json_path)
