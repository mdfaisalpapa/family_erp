import json
import os
import frappe


def sync_child_eb_services(doc, eb_services_list):
	"""
	Idempotently syncs eb_services child table rows on Property doc.
	Updates existing rows matched by service_no, or appends new ones.
	"""
	if not eb_services_list:
		return

	existing_rows_by_no = {r.service_no: r for r in (doc.get("eb_services") or []) if r.service_no}

	for s in eb_services_list:
		s_no = s.get("service_no")
		if not s_no:
			continue

		if s_no in existing_rows_by_no:
			row = existing_rows_by_no[s_no]
			row.consumer_name = s.get("consumer_name", row.consumer_name)
			row.meter_label = s.get("meter_label", row.meter_label)
			row.is_active = s.get("is_active", 1)
		else:
			doc.append("eb_services", {
				"service_no": s_no,
				"consumer_name": s.get("consumer_name"),
				"meter_label": s.get("meter_label"),
				"is_active": s.get("is_active", 1),
			})


def run(json_path=None):
	"""
	Idempotently imports or updates Property records from seed JSON.
	Triggers before_save() to auto-create and link Cost Center under owner_company,
	and syncs eb_services multi-meter child rows.
	"""
	if not json_path:
		bench_path = frappe.utils.get_bench_path()
		json_path = os.path.join(bench_path, "apps", "family_erp", "data", "properties_seed.json")

	if not os.path.exists(json_path):
		frappe.throw(f"Properties seed file not found at: {json_path}")

	with open(json_path, "r", encoding="utf-8") as f:
		properties = json.load(f)

	created_count = 0
	updated_count = 0

	for item in properties:
		p_name = item.get("property_name")
		if not p_name:
			continue

		if frappe.db.exists("Property", p_name):
			doc = frappe.get_doc("Property", p_name)
			for field in [
				"owner_company",
				"property_type",
				"property_tax_assessment_no",
				"water_tax_assessment_no",
				"water_consumer_no",
				"eb_consumer_no",
				"address",
			]:
				if field in item and item[field] is not None:
					doc.set(field, item[field])

			sync_child_eb_services(doc, item.get("eb_services"))
			doc.before_save()
			doc.save(ignore_permissions=True)
			updated_count += 1
		else:
			doc = frappe.new_doc("Property")
			doc.property_name = p_name
			doc.owner_company = item.get("owner_company")
			doc.property_type = item.get("property_type")
			doc.property_tax_assessment_no = item.get("property_tax_assessment_no")
			doc.water_tax_assessment_no = item.get("water_tax_assessment_no")
			doc.water_consumer_no = item.get("water_consumer_no")
			doc.eb_consumer_no = item.get("eb_consumer_no")
			doc.address = item.get("address")
			doc.is_active = item.get("is_active", 1)

			sync_child_eb_services(doc, item.get("eb_services"))
			doc.before_save()
			doc.insert(ignore_permissions=True)
			created_count += 1

	frappe.db.commit()
	print(f"Properties import completed: {created_count} created, {updated_count} updated. Total: {len(properties)}")
	return {"created": created_count, "updated": updated_count, "total": len(properties)}


def verify_isolation(user="farida.test@family.local"):
	"""
	Verifies Property record isolation and child EB service readability for a given user.
	"""
	orig_user = frappe.session.user
	try:
		frappe.set_user(user)
		props = frappe.get_list("Property", fields=["name", "owner_company", "cost_center", "eb_consumer_no"])
		print(f"User: {user}")
		print(f"Total Property records visible: {len(props)}")
		for p in props:
			doc = frappe.get_doc("Property", p.name)
			eb_count = len(doc.get("eb_services") or [])
			print(f"  - {p.name} ({p.owner_company}) -> CC: {p.cost_center} | EB Services: {eb_count}")
		return props
	finally:
		frappe.set_user(orig_user)
