import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt


class FamilyTransaction(Document):
	def validate(self):
		self.validate_amount()
		self.validate_companies()
		mapping = self.fetch_mapping()
		self.set_default_counterparty_account(mapping)
		self.validate_accounts()

	def validate_amount(self):
		if flt(self.amount) <= 0:
			frappe.throw(_("Amount must be greater than zero."))

	def validate_companies(self):
		if not self.primary_company:
			frappe.throw(_("Primary Company is required."))
		if not self.family_company:
			frappe.throw(_("Family Company is required."))
		if self.primary_company == self.family_company:
			frappe.throw(_("Primary Company and Family Company cannot be the same."))

	def fetch_mapping(self):
		settings = frappe.get_single("Family Accounting Settings")
		if not settings.primary_company:
			settings.primary_company = self.primary_company
		return settings.get_mapping(self.family_company)

	def set_default_counterparty_account(self, mapping):
		if self.counterparty_account:
			return

		if self.transaction_type == "Expense Paid on Behalf":
			if mapping.default_expense_account:
				self.counterparty_account = mapping.default_expense_account
			else:
				frappe.throw(
					_(
						"Counterparty Account is required for 'Expense Paid on Behalf' or configure Default Expense Account in Family Accounting Settings for '{0}'."
					).format(self.family_company)
				)
		elif self.transaction_type in ("Settlement Received", "Advance Given"):
			if mapping.default_settlement_account:
				self.counterparty_account = mapping.default_settlement_account
			else:
				frappe.throw(
					_(
						"Counterparty Account is required for '{0}' or configure Default Bank/Cash Account in Family Accounting Settings for '{1}'."
					).format(self.transaction_type, self.family_company)
				)

	def validate_accounts(self):
		if not self.funding_account:
			frappe.throw(_("Primary Bank/Cash/Card Account is required."))
		if not self.counterparty_account:
			frappe.throw(_("Family Expense / Bank Account (Counterparty Account) is required."))

		funding_company = frappe.db.get_value("Account", self.funding_account, "company")
		if funding_company != self.primary_company:
			frappe.throw(
				_(
					"Funding Account '{0}' does not belong to Primary Company '{1}' (belongs to '{2}')."
				).format(self.funding_account, self.primary_company, funding_company)
			)

		counterparty_company = frappe.db.get_value("Account", self.counterparty_account, "company")
		if counterparty_company != self.family_company:
			frappe.throw(
				_(
					"Counterparty Account '{0}' does not belong to Family Company '{1}' (belongs to '{2}')."
				).format(self.counterparty_account, self.family_company, counterparty_company)
			)

	def on_submit(self):
		mapping = self.fetch_mapping()
		remark = f"{self.name}: {self.description or ''}".strip()

		# 1. Primary Company Journal Entry
		primary_je = frappe.new_doc("Journal Entry")
		primary_je.voucher_type = "Journal Entry"
		primary_je.company = self.primary_company
		primary_je.posting_date = self.posting_date
		primary_je.user_remark = remark

		amount = flt(self.amount)
		if self.transaction_type in ("Expense Paid on Behalf", "Advance Given"):
			# Debit Primary Clearing Account, Credit Funding Account
			primary_je.append(
				"accounts",
				{
					"account": mapping.primary_clearing_account,
					"debit_in_account_currency": amount,
					"credit_in_account_currency": 0,
				},
			)
			primary_je.append(
				"accounts",
				{
					"account": self.funding_account,
					"debit_in_account_currency": 0,
					"credit_in_account_currency": amount,
				},
			)
		elif self.transaction_type == "Settlement Received":
			# Debit Funding Account, Credit Primary Clearing Account
			primary_je.append(
				"accounts",
				{
					"account": self.funding_account,
					"debit_in_account_currency": amount,
					"credit_in_account_currency": 0,
				},
			)
			primary_je.append(
				"accounts",
				{
					"account": mapping.primary_clearing_account,
					"debit_in_account_currency": 0,
					"credit_in_account_currency": amount,
				},
			)

		primary_je.flags.ignore_permissions = True
		primary_je.insert()
		primary_je.submit()

		# 2. Family Company Journal Entry
		family_je = frappe.new_doc("Journal Entry")
		family_je.voucher_type = "Journal Entry"
		family_je.company = self.family_company
		family_je.posting_date = self.posting_date
		family_je.user_remark = remark

		if self.transaction_type in ("Expense Paid on Behalf", "Advance Given"):
			# Debit Counterparty Account, Credit Counterparty Clearing Account
			family_je.append(
				"accounts",
				{
					"account": self.counterparty_account,
					"debit_in_account_currency": amount,
					"credit_in_account_currency": 0,
				},
			)
			family_je.append(
				"accounts",
				{
					"account": mapping.counterparty_clearing_account,
					"debit_in_account_currency": 0,
					"credit_in_account_currency": amount,
				},
			)
		elif self.transaction_type == "Settlement Received":
			# Debit Counterparty Clearing Account, Credit Counterparty Account
			family_je.append(
				"accounts",
				{
					"account": mapping.counterparty_clearing_account,
					"debit_in_account_currency": amount,
					"credit_in_account_currency": 0,
				},
			)
			family_je.append(
				"accounts",
				{
					"account": self.counterparty_account,
					"debit_in_account_currency": 0,
					"credit_in_account_currency": amount,
				},
			)

		family_je.flags.ignore_permissions = True
		family_je.insert()
		family_je.submit()

		# Save created journal entries onto self
		self.db_set("primary_journal_entry", primary_je.name)
		self.db_set("counterparty_journal_entry", family_je.name)

	def on_cancel(self):
		self.ignore_linked_doctypes = ("Journal Entry",)

		for je_field in ("counterparty_journal_entry", "primary_journal_entry"):
			je_name = self.get(je_field)
			if je_name and frappe.db.exists("Journal Entry", je_name):
				je_doc = frappe.get_doc("Journal Entry", je_name)
				if je_doc.docstatus == 1:
					je_doc.flags.ignore_permissions = True
					je_doc.cancel()
