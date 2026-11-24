import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt


class QuickExpense(Document):
	def validate(self):
		if flt(self.amount) <= 0:
			frappe.throw(_("Amount must be greater than zero."))

		paid_from_company = frappe.db.get_value("Account", self.paid_from, "company")
		if paid_from_company != self.company:
			frappe.throw(
				_("Paid From account '{0}' does not belong to company '{1}'.").format(
					self.paid_from, self.company
				)
			)

		expense_company = frappe.db.get_value("Account", self.expense_account, "company")
		if expense_company != self.company:
			frappe.throw(
				_("Expense account '{0}' does not belong to company '{1}'.").format(
					self.expense_account, self.company
				)
			)

	def on_submit(self):
		je = frappe.new_doc("Journal Entry")
		je.voucher_type = "Journal Entry"
		je.company = self.company
		je.posting_date = self.posting_date
		je.user_remark = f"{self.name}: {self.description}".strip()

		amt = flt(self.amount)
		# Debit expense_account, Credit paid_from
		je.append(
			"accounts",
			{
				"account": self.expense_account,
				"debit_in_account_currency": amt,
				"credit_in_account_currency": 0,
			},
		)
		je.append(
			"accounts",
			{
				"account": self.paid_from,
				"debit_in_account_currency": 0,
				"credit_in_account_currency": amt,
			},
		)

		je.flags.ignore_permissions = True
		je.insert()
		je.submit()

		self.db_set("journal_entry", je.name)

	def on_cancel(self):
		self.ignore_linked_doctypes = ("Journal Entry",)
		if self.journal_entry and frappe.db.exists("Journal Entry", self.journal_entry):
			je_doc = frappe.get_doc("Journal Entry", self.journal_entry)
			if je_doc.docstatus == 1:
				je_doc.flags.ignore_permissions = True
				je_doc.cancel()
