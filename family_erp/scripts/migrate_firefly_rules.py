# Copyright (c) 2026, Mohammed Faisal and contributors
# For license information, please see license.txt

import json
import re
from collections import defaultdict
import frappe
from frappe.utils import cint
from family_erp.utils.rules_engine import clear_rules_cache

DEFAULT_COMPANY = "Faisal Household"
DEFAULT_EXPORT_PATH = "/tmp/ff_rules_export.json"

STREAMLINED_GROUPS = [
	("Skip / Duplicate Deduplication", 10, "Skips already-recorded salary, rent, EMI, CC, and duplicate UPI/NEFT entries."),
	("Self Transfers & Credit Cards", 20, "Converts internal bank transfers, CC bill payments, and SmartBuy/Gyftr entries."),
	("Family Receivables & TNEB", 30, "Routes utility bills, taxes, and purchases paid on behalf of family receivables."),
	("Income, Cashbacks & Dividends", 40, "Assigns income sources for cashbacks, bank interest, dividends, and refunds."),
	("Investments & Loans", 50, "Tracks mutual fund purchases/redemptions and home loan EMIs."),
	("Taxes & Charity", 60, "Routes local body/income taxes and charity/zakaath payments."),
	("Day-to-Day Expenses & Subscriptions", 70, "Categorizes everyday merchants, groceries, dining, travel, utilities, and gifts."),
]

# Explicit overrides where Firefly account names differ beyond ": " -> " - " + " - FH"
EXPLICIT_ACCOUNT_MAP = {
	"Charity": "zakaath - FH",
	"Entertainment": "entertainment - FH",
	"Gifts Out": "gifts given - FH",
	"Investments : Saba MF (Groww)": "Investment - Saba MF (Groww) - FH",
	"Receivable  : Farida Begum": "Due from Farida Begum - FH",
	"Receivable  : Ikram Mohammed Shah": "Due from Ikram Mohammed Shah - FH",
	"Receivable  : Saad Adil Shah": "Due from Saad Adil Shah - FH",
	"Receivable  : Saira Banu": "Due from Saira Banu - FH",
	"Receivable  : Sakina Sadiqa": "Due from Sakina Sadiqa - FH",
	"Receivable  : Shahi Firdous": "Due from Shahi Firdous - FH",
	"Tax : Local Body": "Taxes - FH",
	"Travel": "travel - FH",
}

TX_TYPE_MAP = {
	"Withdrawal": "Expense",
	"Deposit": "Income",
	"Transfer": "Transfer",
}

SUPPORTED_TRIGGER_TYPES = {
	"transaction_type",
	"description_contains",
	"description_is",
	"description_starts",
	"description_ends",
	"source_account_is",
	"destination_account_is",
	"notes_contains",
	"amount_is",
	"tag_is",
}

# Rules replaced/fixed explicitly for ordering/overlap safety
EXCLUDED_FIREFLY_IDS = {
	93,   # Consolidated into priority rule "9940225510 - Farida Begum LPG & Jio"
	94,   # Consolidated into priority rule "9940225510 - Farida Begum LPG & Jio"
	32,   # Charity Najima overlap -> consolidated into Taxes & Charity
	79,   # Charity Najima overlap -> consolidated into Taxes & Charity
	84,   # Charity Najima overlap -> consolidated into Taxes & Charity
	118,  # Subsumed by EZEKIEL R (#162)
	159,  # Subsumed/merged with wynkauto (#56)
	119,  # Duplicate substring of FIAZ AHA/UBIN/fiazaham (#173)
}


def _norm_ws(s):
	return re.sub(r"\s+", " ", (s or "").strip())


def _build_account_resolver(company=DEFAULT_COMPANY):
	accounts = frappe.get_all(
		"Account",
		filters={"company": company, "is_group": 0},
		fields=["name"],
	)
	lookup = {}
	for a in accounts:
		lookup[a.name.lower()] = a.name
		lookup[_norm_ws(a.name).lower()] = a.name

	unmapped = set()

	def resolve(raw_name):
		if not raw_name or raw_name == "(no name)":
			return None
		if raw_name in EXPLICIT_ACCOUNT_MAP:
			mapped = EXPLICIT_ACCOUNT_MAP[raw_name]
			if mapped.lower() in lookup:
				return lookup[mapped.lower()]
			unmapped.add(f"{raw_name} -> {mapped}")
			return mapped

		# Direct exact or normalized " : " -> " - " + " - FH"
		candidates = [
			raw_name,
			f"{raw_name} - FH",
			re.sub(r"\s*:\s*", " - ", raw_name),
			re.sub(r"\s*:\s*", " - ", raw_name) + " - FH",
		]
		for cand in candidates:
			key = _norm_ws(cand).lower()
			if key in lookup:
				return lookup[key]

		unmapped.add(raw_name)
		cleaned = _norm_ws(re.sub(r"\s*:\s*", " - ", raw_name))
		return f"{cleaned} - FH"

	return resolve, unmapped


def _classify_target_group(orig_gid, ff_rule, actions):
	"""Maps Firefly rule group / rule semantics into one of the 7 streamlined groups."""
	act_types = {a["action_type"] for a in actions}
	act_vals = {a["action_value"] for a in actions}

	if "skip_transaction" in act_types or orig_gid in (4, 16):
		# Note: Cancelled Transactions (#101 in G16) -> convert_transfer Cancelled Transactions - FH sits cleanly in Skip / Self; keep in Skip / Duplicate Deduplication!
		return "Skip / Duplicate Deduplication"

	if any("Tax" in v or v in ("zakaath - FH", "sadaqa - FH") for v in act_vals) or orig_gid in (13, 15):
		return "Taxes & Charity"

	if any(v.startswith("Due from ") for v in act_vals) or orig_gid in (9, 12, 14):
		return "Family Receivables & TNEB"

	if orig_gid in (3, 7) or any(v in ("Refunds and Cashbacks - FH", "Interest - FH", "Dividend Income - FH", "Purchase Refunds - FH") for v in act_vals):
		return "Income, Cashbacks & Dividends"

	if orig_gid == 10 or any(v.startswith("Investment - ") or v.startswith("Loan - ") for v in act_vals):
		return "Investments & Loans"

	if orig_gid in (2, 6, 8) or "convert_transfer" in act_types:
		return "Self Transfers & Credit Cards"

	return "Day-to-Day Expenses & Subscriptions"


def _transform_rule(ff_rule, orig_triggers, orig_actions, resolve_acct):
	actions = []
	for a in orig_actions:
		at = (a.get("action_type") or "").strip()
		av = (a.get("action_value") or "").strip()
		if at in ("set_category", "clear_category"):
			continue
		if at == "delete_transaction":
			actions.append({"action_type": "skip_transaction", "action_value": ""})
		elif at in ("set_source_account", "set_destination_account", "convert_transfer"):
			actions.append({"action_type": at, "action_value": resolve_acct(av) or av})
		elif at in ("add_tag", "remove_all_tags", "set_description", "set_amount"):
			actions.append({"action_type": at, "action_value": av})

	if not actions:
		return None

	triggers = []
	for t in orig_triggers:
		tt = (t.get("trigger_type") or "").strip()
		tv = (t.get("trigger_value") or "").strip()
		if tt in ("user_action", "currency_is", "category_is"):
			continue
		if tt not in SUPPORTED_TRIGGER_TYPES:
			continue
		if tt in ("source_account_is", "destination_account_is"):
			if tv == "(no name)" or not tv:
				continue
			tv = resolve_acct(tv) or tv
		elif tt == "transaction_type":
			tv = TX_TYPE_MAP.get(tv, tv)
		elif tt == "amount_is":
			# Trim trailing decimal noise like 279.000000000000 -> 279
			try:
				fval = float(tv)
				tv = str(int(fval)) if fval == int(fval) else f"{fval:g}"
			except Exception:
				pass

		# Clean trailing whitespace-padded razorpay/zepto exact match -> substring safe
		if ff_rule["id"] == 48 and tt == "description_is" and tv == "Paid on Zepto Marketplace":
			tt = "description_contains"
			tv = "Zepto"

		triggers.append({"trigger_type": tt, "trigger_value": tv, "negated": 0})

	# Deduplicate identical triggers within the rule
	seen_t = set()
	dedup_triggers = []
	for t in triggers:
		k = (t["trigger_type"], t["trigger_value"], t["negated"])
		if k not in seen_t:
			seen_t.add(k)
			dedup_triggers.append(t)

	if not dedup_triggers:
		return None

	target_group = _classify_target_group(cint(ff_rule["rule_group_id"]), ff_rule, actions)
	is_skip = any(a["action_type"] == "skip_transaction" for a in actions)

	return {
		"orig_id": ff_rule["id"],
		"title": (ff_rule.get("title") or "").strip()[:140],
		"rule_group": target_group,
		"sort_order": 50 + cint(ff_rule.get("order")) * 10,
		"match_all": 1,
		"stop_processing": 1 if is_skip else 0,
		"triggers": dedup_triggers,
		"actions": actions,
	}


def _extract_mergeable_keyword(rule_dict):
	"""
	If rule has only optional context triggers (transaction_type, source_account_is, destination_account_is)
	plus exactly 1 text trigger in (description_contains, description_is, notes_contains),
	returns (compat_sig, text_trigger_field, keyword).
	"""
	text_trigs = []
	ctx_trigs = []
	for t in rule_dict["triggers"]:
		if t["trigger_type"] in ("transaction_type", "source_account_is", "destination_account_is"):
			ctx_trigs.append((t["trigger_type"], t["trigger_value"]))
		elif t["trigger_type"] in ("description_contains", "description_is", "notes_contains"):
			text_trigs.append(t)
		else:
			return None

	if len(text_trigs) != 1:
		return None

	txt = text_trigs[0]
	merge_field = "notes_contains" if txt["trigger_type"] == "notes_contains" else "description_contains"
	act_sig = tuple(sorted((a["action_type"], a["action_value"]) for a in rule_dict["actions"]))
	ctx_sig = tuple(sorted(ctx_trigs))
	return ((rule_dict["rule_group"], act_sig, ctx_sig, merge_field), rule_dict["actions"], ctx_trigs, merge_field, txt["trigger_value"])


def _build_consolidated_rules(export_path=DEFAULT_EXPORT_PATH, company=DEFAULT_COMPANY):
	with open(export_path, "r", encoding="utf-8") as f:
		data = json.load(f)

	resolve_acct, unmapped = _build_account_resolver(company)
	trig_by_rule = defaultdict(list)
	for t in data["rule_triggers"]:
		trig_by_rule[t["rule_id"]].append(t)
	act_by_rule = defaultdict(list)
	for a in data["rule_actions"]:
		act_by_rule[a["rule_id"]].append(a)

	transformed = []
	for r in data["rules"]:
		if r["id"] in EXCLUDED_FIREFLY_IDS:
			continue
		tr = _transform_rule(r, trig_by_rule[r["id"]], act_by_rule[r["id"]], resolve_acct)
		if tr:
			transformed.append(tr)

	# Group mergeable rules by (rule_group, action_sig, ctx_sig, merge_field)
	merge_buckets = defaultdict(list)
	standalone = []
	for tr in transformed:
		info = _extract_mergeable_keyword(tr)
		if info:
			key, actions, ctx_trigs, merge_field, kw = info
			merge_buckets[key].append((tr, actions, ctx_trigs, merge_field, kw))
		else:
			standalone.append(tr)

	final_rules = []

	# 1. Explicit hazard fixes (ordered first in their groups)
	final_rules.append({
		"title": "Farida Begum - LPG & Jio 9940225510 (Before Sakina)",
		"rule_group": "Family Receivables & TNEB",
		"sort_order": 5,
		"match_all": 1,
		"stop_processing": 1,
		"triggers": [
			{"trigger_type": "transaction_type", "trigger_value": "Expense", "negated": 0},
			{"trigger_type": "description_contains", "trigger_value": "9940225510", "negated": 0},
		],
		"actions": [
			{"action_type": "convert_transfer", "action_value": resolve_acct("Receivable  : Farida Begum")},
		],
	})

	final_rules.append({
		"title": "Charity - Najima / Buxuniisa",
		"rule_group": "Taxes & Charity",
		"sort_order": 10,
		"match_all": 1,
		"stop_processing": 1,
		"triggers": [
			{"trigger_type": "transaction_type", "trigger_value": "Expense", "negated": 0},
			{"trigger_type": "description_contains", "trigger_value": "najimasyed@okaxis | najimas", "negated": 0},
		],
		"actions": [
			{"action_type": "set_destination_account", "action_value": resolve_acct("Charity")},
			{"action_type": "add_tag", "action_value": "Buxuniisa"},
		],
	})

	# 2. Emit merged pipe-separated rules
	for key, members in merge_buckets.items():
		rule_group, _act_sig, _ctx_sig, merge_field = key
		actions = members[0][1]
		ctx_trigs = members[0][2]
		is_skip = any(a["action_type"] == "skip_transaction" for a in actions)

		if len(members) == 1:
			tr = members[0][0]
			# Preserve original standalone title cleanly
			final_rules.append({
				"title": tr["title"],
				"rule_group": rule_group,
				"sort_order": tr["sort_order"],
				"match_all": 1,
				"stop_processing": tr["stop_processing"],
				"triggers": tr["triggers"],
				"actions": actions,
			})
			continue

		# Deduplicate keywords preserving order
		kws = []
		kw_seen = set()
		for _, _, _, _, kw in members:
			if kw.lower() not in kw_seen:
				kw_seen.add(kw.lower())
				kws.append(kw)

		# Summarize title nicely from action target
		target_lbl = ", ".join(
			a["action_value"].replace(" - FH", "") if a["action_value"] else a["action_type"]
			for a in actions
		)
		tx_lbl = next((v for k, v in ctx_trigs if k == "transaction_type"), "Any")
		src_lbl = next((v.replace(" - FH", "") for k, v in ctx_trigs if k == "source_account_is"), "")
		dst_lbl = next((v.replace(" - FH", "") for k, v in ctx_trigs if k == "destination_account_is"), "")
		extra_ctx = " / ".join(x for x in (tx_lbl, src_lbl, dst_lbl) if x)
		merged_title = f"{target_lbl} ({extra_ctx}) [{len(kws)} patterns]"[:140]

		# Keep SmartBuy/TNEB/Skip priority ordering clean
		base_sort = min(m[0]["sort_order"] for m in members)
		# Put Farida general rule after 9940225510 rule (sort_order >= 20)
		if "Due from Farida Begum - FH" in str(actions):
			base_sort = max(base_sort, 25)
		if "Due from Sakina Sadiqa - FH" in str(actions):
			base_sort = max(base_sort, 35)

		trigs = [{"trigger_type": tt, "trigger_value": tv, "negated": 0} for tt, tv in ctx_trigs]
		trigs.append({"trigger_type": merge_field, "trigger_value": " | ".join(kws), "negated": 0})

		final_rules.append({
			"title": merged_title,
			"rule_group": rule_group,
			"sort_order": base_sort,
			"match_all": 1,
			"stop_processing": 1 if is_skip else 0,
			"triggers": trigs,
			"actions": actions,
		})

	# 3. Emit standalone non-mergeable rules
	for tr in standalone:
		# Ensure specific SmartBuy exact amount rule (#114) runs before generic GYFTR rules
		sort_ord = tr["sort_order"]
		if tr.get("orig_id") == 114:
			sort_ord = 5
		if tr.get("orig_id") == 26:
			sort_ord = 20
		final_rules.append({
			"title": tr["title"],
			"rule_group": tr["rule_group"],
			"sort_order": sort_ord,
			"match_all": tr["match_all"],
			"stop_processing": tr["stop_processing"],
			"triggers": tr["triggers"],
			"actions": tr["actions"],
		})

	# Ensure unique rule titles inside each group (and overall) so idempotent upsert is unambiguous
	title_counts = defaultdict(int)
	for r in final_rules:
		t = r["title"]
		title_counts[t] += 1
		if title_counts[t] > 1:
			r["title"] = f"{t[:132]} #{title_counts[t]}"

	return final_rules, sorted(unmapped)


def run(export_path=DEFAULT_EXPORT_PATH, company=DEFAULT_COMPANY, dry_run=0):
	"""
	Idempotently migrates simplified 7-group Firefly III automation rules into
	Family Rule Group and Family Automation Rule DocTypes.
	"""
	dry_run = cint(dry_run)
	final_rules, unmapped = _build_consolidated_rules(export_path=export_path, company=company)
	total_triggers = sum(len(r["triggers"]) for r in final_rules)
	total_actions = sum(len(r["actions"]) for r in final_rules)

	print(f"Prepared {len(STREAMLINED_GROUPS)} groups, {len(final_rules)} consolidated rules, {total_triggers} triggers, {total_actions} actions")
	print(f"Unmapped accounts ({len(unmapped)}): {unmapped}")

	if dry_run:
		by_grp = defaultdict(list)
		for r in final_rules:
			by_grp[r["rule_group"]].append(r)
		for gtitle, _, _ in STREAMLINED_GROUPS:
			print(f"  Group '{gtitle}': {len(by_grp[gtitle])} rules")
		return {
			"dry_run": True,
			"groups": len(STREAMLINED_GROUPS),
			"rules": len(final_rules),
			"triggers": total_triggers,
			"unmapped": unmapped,
		}

	# 1. Upsert the 7 streamlined groups cleanly
	target_group_titles = {g[0] for g in STREAMLINED_GROUPS}
	for title, sort_order, desc in STREAMLINED_GROUPS:
		if frappe.db.exists("Family Rule Group", title):
			gdoc = frappe.get_doc("Family Rule Group", title)
			gdoc.sort_order = sort_order
			gdoc.is_active = 1
			gdoc.description = desc
			gdoc.flags.ignore_permissions = True
			gdoc.save()
		else:
			gdoc = frappe.get_doc({
				"doctype": "Family Rule Group",
				"title": title,
				"sort_order": sort_order,
				"is_active": 1,
				"description": desc,
			})
			gdoc.flags.ignore_permissions = True
			gdoc.insert()

	# 2. Upsert rules idempotently by (rule_group, title) and remove stale migrated rules not in target set
	desired_keys = {(r["rule_group"], r["title"]) for r in final_rules}
	existing_rules = frappe.get_all(
		"Family Automation Rule",
		fields=["name", "rule_group", "title"],
	)
	existing_by_key = {(r.rule_group, r.title): r.name for r in existing_rules}

	created, updated, deleted = 0, 0, 0
	for er in existing_rules:
		if (er.rule_group, er.title) not in desired_keys:
			frappe.delete_doc("Family Automation Rule", er.name, ignore_permissions=True)
			deleted += 1

	for er_grp in frappe.get_all("Family Rule Group", pluck="name"):
		if er_grp not in target_group_titles:
			frappe.delete_doc("Family Rule Group", er_grp, ignore_permissions=True)

	for r in final_rules:
		key = (r["rule_group"], r["title"])
		if key in existing_by_key:
			doc = frappe.get_doc("Family Automation Rule", existing_by_key[key])
			doc.sort_order = r["sort_order"]
			doc.is_active = 1
			doc.match_all = r["match_all"]
			doc.stop_processing = r["stop_processing"]
			doc.set("triggers", r["triggers"])
			doc.set("actions", r["actions"])
			doc.flags.ignore_permissions = True
			doc.save()
			updated += 1
		else:
			doc = frappe.get_doc({
				"doctype": "Family Automation Rule",
				"title": r["title"],
				"rule_group": r["rule_group"],
				"sort_order": r["sort_order"],
				"is_active": 1,
				"match_all": r["match_all"],
				"stop_processing": r["stop_processing"],
				"triggers": r["triggers"],
				"actions": r["actions"],
			})
			doc.flags.ignore_permissions = True
			doc.insert()
			created += 1

	frappe.db.commit()
	clear_rules_cache()

	summary = {
		"groups": frappe.db.count("Family Rule Group"),
		"rules": frappe.db.count("Family Automation Rule"),
		"triggers": frappe.db.count("Family Rule Trigger"),
		"actions": frappe.db.count("Family Rule Action"),
		"created": created,
		"updated": updated,
		"deleted": deleted,
		"unmapped": unmapped,
	}
	print("Migration summary:", summary)
	return summary
