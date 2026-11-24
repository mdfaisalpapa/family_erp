frappe.ui.form.on("Family Transaction", {
	onload(frm) {
		if (frm.is_new() && !frm.doc.primary_company) {
			frappe.db.get_single_value("Family Accounting Settings", "primary_company").then((val) => {
				if (val && !frm.doc.primary_company) {
					frm.set_value("primary_company", val);
				}
			});
		}
	},

	setup(frm) {
		frm.set_query("funding_account", function () {
			return {
				filters: {
					company: frm.doc.primary_company || "",
					is_group: 0,
				},
			};
		});

		frm.set_query("counterparty_account", function () {
			return {
				filters: {
					company: frm.doc.family_company || "",
					is_group: 0,
				},
			};
		});
	},

	primary_company(frm) {
		frm.set_query("funding_account", function () {
			return {
				filters: {
					company: frm.doc.primary_company || "",
					is_group: 0,
				},
			};
		});
	},

	family_company(frm) {
		frm.set_query("counterparty_account", function () {
			return {
				filters: {
					company: frm.doc.family_company || "",
					is_group: 0,
				},
			};
		});
	},
});
