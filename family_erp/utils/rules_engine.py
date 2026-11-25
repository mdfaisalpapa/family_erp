# Copyright (c) 2026, Mohammed Faisal and contributors
# For license information, please see license.txt

import copy
import frappe
from frappe.utils import cint, flt

ACTIVE_RULES_CACHE_KEY = "family_erp:active_automation_rules"


def clear_rules_cache():
	"""Clears cached active Family Automation Rules."""
	frappe.cache().delete_value(ACTIVE_RULES_CACHE_KEY)


def serialize_rule_doc(doc, group_title="", group_sort_order=0):
	"""Converts a Family Automation Rule Document (or dict) into a plain dict."""
	triggers = [
		{
			"trigger_type": (t.get("trigger_type") or "").strip(),
			"trigger_value": "" if t.get("trigger_value") is None else str(t.get("trigger_value")),
			"negated": cint(t.get("negated")),
		}
		for t in (doc.get("triggers") or [])
	]
	actions = [
		{
			"action_type": (a.get("action_type") or "").strip(),
			"action_value": "" if a.get("action_value") is None else str(a.get("action_value")),
		}
		for a in (doc.get("actions") or [])
	]
	return {
		"name": doc.get("name"),
		"title": doc.get("title") or "",
		"rule_group": doc.get("rule_group") or "",
		"group_title": group_title or doc.get("rule_group") or "",
		"group_sort_order": cint(group_sort_order),
		"sort_order": cint(doc.get("sort_order")),
		"match_all": cint(doc.get("match_all", 1)),
		"stop_processing": cint(doc.get("stop_processing", 0)),
		"triggers": triggers,
		"actions": actions,
	}


def get_active_rules():
	"""
	Returns active Family Automation Rules belonging to active Family Rule Groups,
	ordered by group.sort_order ASC, rule.sort_order ASC, rule.name ASC, cached via frappe.cache().
	"""
	cached = frappe.cache().get_value(ACTIVE_RULES_CACHE_KEY)
	if isinstance(cached, list):
		return cached

	groups = frappe.get_all(
		"Family Rule Group",
		filters={"is_active": 1},
		fields=["name", "title", "sort_order"],
	)
	if not groups:
		frappe.cache().set_value(ACTIVE_RULES_CACHE_KEY, [])
		return []

	group_map = {g.name: g for g in groups}
	rules = frappe.get_all(
		"Family Automation Rule",
		filters={"is_active": 1, "rule_group": ["in", list(group_map.keys())]},
		fields=["name", "title", "rule_group", "sort_order", "match_all", "stop_processing"],
	)
	if not rules:
		frappe.cache().set_value(ACTIVE_RULES_CACHE_KEY, [])
		return []

	rule_names = [r.name for r in rules]
	trigger_rows = frappe.get_all(
		"Family Rule Trigger",
		filters={"parenttype": "Family Automation Rule", "parent": ["in", rule_names]},
		fields=["parent", "trigger_type", "trigger_value", "negated", "idx"],
		order_by="idx asc",
	)
	action_rows = frappe.get_all(
		"Family Rule Action",
		filters={"parenttype": "Family Automation Rule", "parent": ["in", rule_names]},
		fields=["parent", "action_type", "action_value", "idx"],
		order_by="idx asc",
	)

	trig_by_rule = {}
	for t in trigger_rows:
		trig_by_rule.setdefault(t.parent, []).append(
			{
				"trigger_type": (t.trigger_type or "").strip(),
				"trigger_value": "" if t.trigger_value is None else str(t.trigger_value),
				"negated": cint(t.negated),
			}
		)

	act_by_rule = {}
	for a in action_rows:
		act_by_rule.setdefault(a.parent, []).append(
			{
				"action_type": (a.action_type or "").strip(),
				"action_value": "" if a.action_value is None else str(a.action_value),
			}
		)

	out = []
	for r in rules:
		grp = group_map.get(r.rule_group)
		out.append(
			{
				"name": r.name,
				"title": r.title or "",
				"rule_group": r.rule_group,
				"group_title": grp.title if grp else r.rule_group,
				"group_sort_order": cint(grp.sort_order) if grp else 0,
				"sort_order": cint(r.sort_order),
				"match_all": cint(r.match_all),
				"stop_processing": cint(r.stop_processing),
				"triggers": trig_by_rule.get(r.name, []),
				"actions": act_by_rule.get(r.name, []),
			}
		)

	out.sort(key=lambda x: (x["group_sort_order"], x["sort_order"], x["name"] or ""))
	frappe.cache().set_value(ACTIVE_RULES_CACHE_KEY, out)
	return out


def _normalize_tags(raw_tags):
	if not raw_tags:
		return []
	if isinstance(raw_tags, (list, tuple, set)):
		parts = [str(x).strip() for x in raw_tags]
	else:
		parts = [x.strip() for x in str(raw_tags).split(",")]
	seen = []
	lower_seen = set()
	for p in parts:
		if p and p.lower() not in lower_seen:
			seen.append(p)
			lower_seen.add(p.lower())
	return seen


def _pipe_tokens(raw_val):
	"""Splits pipe-separated OR keywords, stripping whitespace and ignoring empty tokens."""
	return [tok.strip().lower() for tok in raw_val.split("|") if tok.strip()]


def _match_trigger(trigger, payload):
	t_type = (trigger.get("trigger_type") or "").strip()
	raw_val = "" if trigger.get("trigger_value") is None else str(trigger.get("trigger_value")).strip()
	val_l = raw_val.lower()
	negated = cint(trigger.get("negated")) == 1

	desc = str(payload.get("description") or "").strip()
	desc_l = desc.lower()
	tx_type = str(payload.get("transaction_type") or "").strip().lower()
	src_acct = str(payload.get("source_account") or "").strip().lower()
	dst_acct = str(payload.get("destination_account") or "").strip().lower()
	notes_l = str(payload.get("notes") or "").lower()

	matched = False
	if t_type == "transaction_type":
		matched = tx_type == val_l
	elif t_type == "description_contains":
		tokens = _pipe_tokens(raw_val)
		matched = any(tok in desc_l for tok in tokens)
	elif t_type == "description_is":
		matched = desc_l == val_l
	elif t_type == "description_starts":
		tokens = _pipe_tokens(raw_val)
		matched = any(desc_l.startswith(tok) for tok in tokens)
	elif t_type == "description_ends":
		tokens = _pipe_tokens(raw_val)
		matched = any(desc_l.endswith(tok) for tok in tokens)
	elif t_type == "source_account_is":
		matched = src_acct == val_l
	elif t_type == "destination_account_is":
		matched = dst_acct == val_l
	elif t_type == "notes_contains":
		tokens = _pipe_tokens(raw_val)
		matched = any(tok in notes_l for tok in tokens)
	elif t_type == "amount_is":
		amt_raw = payload.get("amount")
		if amt_raw is not None and raw_val:
			try:
				matched = abs(flt(amt_raw) - flt(raw_val)) < 0.005
			except Exception:
				matched = str(amt_raw).strip().lower() == val_l
	elif t_type == "tag_is":
		tags_l = {t.lower() for t in _normalize_tags(payload.get("tags"))}
		matched = val_l in tags_l

	return (not matched) if negated else matched


_evaluate_trigger = _match_trigger


def _rule_matches(rule, payload):
	triggers = rule.get("triggers") or []
	if not triggers:
		return False
	if cint(rule.get("match_all", 1)):
		return all(_match_trigger(t, payload) for t in triggers)
	return any(_match_trigger(t, payload) for t in triggers)


def _apply_rule_actions(rule, payload):
	skipped = False
	applied = []
	for act in rule.get("actions") or []:
		a_type = (act.get("action_type") or "").strip()
		a_val = "" if act.get("action_value") is None else str(act.get("action_value")).strip()

		if a_type == "set_source_account":
			payload["source_account"] = a_val
		elif a_type == "set_destination_account":
			payload["destination_account"] = a_val
		elif a_type == "convert_transfer":
			orig_type = str(payload.get("transaction_type") or "").strip().lower()
			payload["transaction_type"] = "Transfer"
			if a_val:
				if orig_type in ("income", "deposit"):
					payload["source_account"] = a_val
				else:
					payload["destination_account"] = a_val
		elif a_type == "add_tag":
			if a_val:
				cur_tags = _normalize_tags(payload.get("tags"))
				if a_val.lower() not in {t.lower() for t in cur_tags}:
					cur_tags.append(a_val)
				payload["tags"] = cur_tags
		elif a_type == "remove_all_tags":
			payload["tags"] = []
		elif a_type == "set_description":
			payload["description"] = a_val
		elif a_type == "set_amount":
			payload["amount"] = flt(a_val, 2)
		elif a_type == "skip_transaction":
			skipped = True
		else:
			continue

		applied.append({"action_type": a_type, "action_value": a_val})
	return skipped, applied


def evaluate_rules(tx_payload, company=None, rules=None):
	"""
	Evaluates active automation rules (or provided rules) against tx_payload in priority order.
	Returns {"payload": mutated_payload, "matched_rules": [...], "skipped": bool}.
	"""
	mutated = copy.deepcopy(tx_payload or {})
	mutated["tags"] = _normalize_tags(mutated.get("tags"))
	if company and not mutated.get("company"):
		mutated["company"] = company

	rule_list = rules if rules is not None else get_active_rules()
	matched_rules = []
	skipped = False

	for rule in rule_list:
		if not _rule_matches(rule, mutated):
			continue
		rule_skipped, applied_actions = _apply_rule_actions(rule, mutated)
		if rule_skipped:
			skipped = True
		matched_rules.append(
			{
				"rule_name": rule.get("name"),
				"title": rule.get("title"),
				"rule_group": rule.get("rule_group"),
				"actions": applied_actions,
			}
		)
		if cint(rule.get("stop_processing")):
			break

	return {
		"payload": mutated,
		"matched_rules": matched_rules,
		"skipped": skipped,
	}
