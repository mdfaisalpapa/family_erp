frappe.ui.form.on("Quick Expense", {
	onload(frm) {
		if (frm.is_new() && !frm.doc.company) {
			frappe.db.get_single_value("Family Accounting Settings", "primary_company").then((val) => {
				if (val && !frm.doc.company) {
					frm.set_value("company", val);
				}
			});
		}
	},

	setup(frm) {
		frm.set_query("paid_from", function () {
			return {
				filters: {
					company: frm.doc.company || "",
					is_group: 0,
				},
			};
		});

		frm.set_query("expense_account", function () {
			return {
				filters: {
					company: frm.doc.company || "",
					is_group: 0,
				},
			};
		});
	},

	company(frm) {
		frm.set_query("paid_from", function () {
			return {
				filters: {
					company: frm.doc.company || "",
					is_group: 0,
				},
			};
		});

		frm.set_query("expense_account", function () {
			return {
				filters: {
					company: frm.doc.company || "",
					is_group: 0,
				},
			};
		});
	},
});
