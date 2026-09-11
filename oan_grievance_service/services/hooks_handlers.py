"""Document event handlers registered in hooks.py.

These are the joins between a saved record and the workflow the FSD describes, kept
out of the doctype controllers so the sequence is readable in one place.
"""

import frappe
from frappe import _
from frappe.utils import now_datetime

from oan_grievance_service.services import constants as C
from oan_grievance_service.services import lifecycle, notifications, routing, sla


def grievance_after_insert(doc, method=None):
	"""FSD 4.1 steps 6-8: acknowledge, then route or queue for the nodal officer."""
	if frappe.flags.in_install or frappe.flags.in_migrate:
		return
	notifications.queue(doc, C.EVENT_SUBMISSION_RECEIVED)
	routing.apply_routing(doc)


def response_after_insert(doc, method=None):
	"""FSD 3.5 and Appendix D-2: the response outcome drives the next status."""
	grievance = frappe.get_doc("Grievance", doc.grievance)

	# D-3: response_date, responded_by, sequence and prior_status are filled in by the
	# controller before validation, because they are mandatory. The IP is captured here
	# because it is only meaningful for a request that actually reached the server.
	if getattr(frappe.local, "request_ip", None):
		doc.db_set("ip_address", frappe.local.request_ip, update_modified=False)

	next_status = C.RESPONSE_OUTCOME_NEXT_STATUS.get(doc.response_type)
	if next_status and next_status != grievance.status:
		lifecycle.change_status(grievance, next_status, note=f"Response {doc.name} ({doc.response_type})")

	doc.db_set("new_status", next_status or grievance.status, update_modified=False)

	# FSD 4.3: a structured response clears the escalation flag.
	sla.clear_escalation(grievance)

	notifications.queue(grievance, C.EVENT_RESPONSE_SENT)
	doc.db_set("notification_sent", 1, update_modified=False)
	doc.db_set("notification_sent_at", now_datetime(), update_modified=False)


def reassignment_on_update(doc, method=None):
	"""FSD 3.3.1: the target office gets no rights until L2 approves.

	The reassignment is committed here, on approval, and nowhere else, which is what
	makes 'the reassigned officer may act only after the approved assignment is
	committed' true rather than aspirational.
	"""
	from oan_grievance_service.permissions import can_approve_reassignment

	if doc.decision == "Pending":
		notifications.queue(frappe.get_doc("Grievance", doc.grievance), C.EVENT_REASSIGNMENT_REQUESTED)
		return

	if doc.get_doc_before_save() and doc.get_doc_before_save().decision != "Pending":
		return

	if not can_approve_reassignment():
		frappe.throw(
			_("Only a supervising officer may approve or reject a reassignment."),
			title=_("Approval Not Permitted"),
		)

	doc.db_set("decided_at", now_datetime(), update_modified=False)
	doc.db_set("approver", frappe.session.user, update_modified=False)

	if doc.decision != "Approved":
		return

	grievance = frappe.get_doc("Grievance", doc.grievance)
	grievance.db_set("assigned_dept", doc.target_department, update_modified=False)
	if doc.target_officer:
		grievance.db_set("assigned_to", doc.target_officer, update_modified=False)

	# FSD 3.3.1: SLA treatment follows configured policy and is never implicit.
	# Appendix D-2 assumes a reset for a referral; 3.3.1 makes it a decision. The
	# field carries that decision, and an unset field means the clock continues.
	if doc.sla_treatment == "Reset":
		grievance.db_set("sla_due_date", None, update_modified=False)
		grievance.db_set("sla_start_at", None, update_modified=False)
		grievance.db_set("reminder_50_sent", 0, update_modified=False)
		grievance.db_set("reminder_80_sent", 0, update_modified=False)
		sla.start_clock(grievance)

	frappe.get_doc(
		{
			"doctype": "Grievance Status History",
			"grievance": grievance.name,
			"from_status": grievance.status,
			"to_status": grievance.status,
			"changed_by": frappe.session.user,
			"timestamp": now_datetime(),
			"notes": f"Reassigned to {doc.target_department} (SLA {doc.sla_treatment or 'Continue'})",
		}
	).insert(ignore_permissions=True)


def deferral_on_update(doc, method=None):
	"""FSD 3.11.7: an approved deferral extends the SLA window."""
	from oan_grievance_service.permissions import can_approve_deferral

	if doc.status == "Pending":
		return
	before = doc.get_doc_before_save()
	if before and before.status != "Pending":
		return

	if not can_approve_deferral():
		frappe.throw(
			_("Only a supervising officer may decide a deferral."),
			title=_("Approval Not Permitted"),
		)

	doc.db_set("approver", frappe.session.user, update_modified=False)
	doc.db_set("decided_at", now_datetime(), update_modified=False)

	if doc.status != "Approved":
		return

	max_days = frappe.conf.get("grievance_max_deferral_days") or C.DEFAULT_MAX_DEFERRAL_DAYS
	if doc.additional_days > max_days:
		frappe.throw(
			_("A deferral may not exceed {0} days.").format(max_days),
			title=_("Deferral Too Long"),
		)

	grievance = frappe.get_doc("Grievance", doc.grievance)
	sla.extend_for_deferral(grievance, doc.additional_days)


def anonymity_on_update(doc, method=None):
	"""FSD 9.2: approval masks the submitter from department officers."""
	if doc.status == "Pending":
		return
	before = doc.get_doc_before_save()
	if before and before.status != "Pending":
		return

	doc.db_set("decided_by", frappe.session.user, update_modified=False)
	doc.db_set("decided_at", now_datetime(), update_modified=False)

	grievance = frappe.get_doc("Grievance", doc.grievance)
	grievance.db_set("anonymity_status", doc.status, update_modified=False)

	if doc.status == "Approved":
		grievance.db_set("is_anonymous", 1, update_modified=False)
		grievance.db_set("anonymity_approved_by", frappe.session.user, update_modified=False)
	elif doc.status == "Rejected":
		grievance.db_set("is_anonymous", 0, update_modified=False)
		lifecycle.reject(grievance, "Anonymity refused and identity not disclosed")


def on_user_registered(user_doc, role=None, roles=None, **kwargs):
	"""Handle user registration event broadcast from oan_auth_service.

	If 'Grievance Submitter' is among the assigned roles:
	1. Resolves submitter_type (defaulting to 'Individual Farmer').
	2. Validates that the submitter type exists.
	3. Derives and validates the canonical dedupe_key based on scheme rules.
	4. Populates general contact and submitter-type-specific fields.
	5. Creates or updates and links the Submitter Profile record to the User.
	"""
	from oan_grievance_service.grievance_masters.doctype.submitter_profile.submitter_profile import (
		build_dedupe_key,
	)
	from oan_grievance_service.services import identity

	assigned_roles = set(roles or [])
	if role:
		assigned_roles.add(role)

	# Only process if user is registering as a Grievance Submitter
	if "Grievance Submitter" not in assigned_roles:
		return None

	submitter_type = (kwargs.get("submitter_type") or "Individual Farmer").strip()

	if not frappe.db.exists("Submitter Type", submitter_type):
		frappe.throw(
			_("Submitter Type '{0}' does not exist.").format(submitter_type),
			frappe.ValidationError,
		)

	submitter_name = (
		kwargs.get("submitter_name")
		or kwargs.get("full_name")
		or f"{user_doc.first_name or ''} {user_doc.last_name or ''}".strip()
		or user_doc.name
	)

	contact_mobile = (
		kwargs.get("contact_mobile") or kwargs.get("phone_number") or user_doc.mobile_no or ""
	).strip()

	contact_email = (kwargs.get("contact_email") or kwargs.get("email") or "").strip() or None

	if not contact_email and user_doc.email and not user_doc.email.endswith("@id.openagrinet.internal"):
		contact_email = user_doc.email

	# Notification language belongs on the User record, the one identity primitive shared by
	# submitters and staff. Guarded because a bench need not have the Language record seeded.
	preferred_language = kwargs.get("preferred_language")
	if preferred_language and frappe.db.exists("Language", preferred_language):
		user_doc.db_set("language", preferred_language, update_modified=False)

	# Automatically derive dedupe_key from inputs (fayda_id, registration_number, farmer_id, phone, etc.)
	dedupe_key = identity.derive_dedupe_key(
		submitter_type=submitter_type,
		mobile=contact_mobile,
		fayda_id=kwargs.get("fayda_id"),
		national_id=kwargs.get("national_id"),
		registration_number=kwargs.get("registration_number"),
		org_number=kwargs.get("org_number"),
		farmer_id=kwargs.get("farmer_id"),
		dedupe_key=kwargs.get("dedupe_key"),
	)

	if not dedupe_key:
		frappe.throw(
			_("Submitter registration requires a valid contact phone number or identifier."),
			frappe.ValidationError,
		)

	# Check if a Submitter Profile already exists with this dedupe_key
	existing_name = frappe.db.get_value("Submitter Profile", {"dedupe_key": dedupe_key}, "name")
	if existing_name:
		profile = frappe.get_doc("Submitter Profile", existing_name)
		if profile.user and profile.user != user_doc.name:
			frappe.throw(
				_(
					"A Submitter Profile with dedupe key '{0}' is already registered under another account."
				).format(dedupe_key),
				frappe.DuplicateEntryError,
			)
		profile.user = user_doc.name
		if submitter_name:
			profile.submitter_name = submitter_name
		if contact_mobile:
			profile.contact_mobile = contact_mobile
		if contact_email:
			profile.contact_email = contact_email
		admin_area = kwargs.get("administrative_area") or kwargs.get("region")
		if admin_area:
			profile.administrative_area = admin_area
		if kwargs.get("administrative_unit") or kwargs.get("woreda"):
			profile.administrative_unit = kwargs.get("administrative_unit") or kwargs.get("woreda")
		profile.save(ignore_permissions=True)
	else:
		profile = frappe.new_doc("Submitter Profile")
		profile.user = user_doc.name
		profile.submitter_type = submitter_type
		profile.submitter_name = submitter_name
		profile.contact_mobile = contact_mobile
		profile.contact_email = contact_email
		profile.dedupe_key = dedupe_key
		profile.administrative_area = kwargs.get("administrative_area") or kwargs.get("region")
		profile.administrative_unit = kwargs.get("administrative_unit") or kwargs.get("woreda")
		profile.active = 1

		profile.insert(ignore_permissions=True)

	return profile
