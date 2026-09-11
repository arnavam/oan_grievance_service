# Copyright (c) 2026, COSS - Centre for Open Societal Systems and Contributors
# See license.txt

"""FR-08 acceptance tests.

These cover the behaviour that is ours rather than core's: that a lifecycle event
produces exactly one queued row per recipient, that re-firing does not duplicate it,
that a disabled or condition-failing Notification produces nothing, that each
Appendix C role resolves through the Link hops core cannot follow, and that a log row
cannot be deleted by anyone.
"""

import frappe
from frappe.tests.utils import FrappeTestCase

from oan_grievance_service.services import notifications

EVENT = "test_notification_event"


def _ensure_user(email, mobile=None, language=None):
	if not frappe.db.exists("User", email):
		frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": email.split("@")[0],
				"send_welcome_email": 0,
				"mobile_no": mobile,
				"language": language,
			}
		).insert(ignore_permissions=True)
	return email


class TestGrievanceNotificationLog(FrappeTestCase):
	def setUp(self):
		self.head = _ensure_user("notif-head@example.com", "+251900000001")
		self.nodal = _ensure_user("notif-nodal@example.com", "+251900000002")
		self.senior = _ensure_user("notif-senior@example.com", "+251900000003")

		dept_name = frappe.db.get_value("Grievance Department", {"dept_name": "Notif Test Dept"}, "name")
		if not dept_name:
			self.dept = frappe.get_doc(
				{
					"doctype": "Grievance Department",
					"dept_name": "Notif Test Dept",
					"email_account": "notif-dept@example.com",
					"head_of_dept": self.head,
					"nodal_officer": self.nodal,
					"senior_officer": self.senior,
					"active": 1,
				}
			).insert(ignore_permissions=True)
		else:
			self.dept = frappe.get_doc("Grievance Department", dept_name)

		self.area = self._ensure_area()
		self.gtype = self._ensure_grievance_type()

		self.grievance = frappe.get_doc(
			{
				"doctype": "Grievance",
				"submitter_type": "Individual Farmer",
				"submitter_name": "Notif Test Submitter",
				"submission_channel": "Mobile App",
				"administrative_area": self.area,
				"service_category": "Inputs",
				"grievance_type": self.gtype,
				"description": "Notification log test",
				"assigned_dept": self.dept.name,
				"contact_mobile": "+251911111111",
				"contact_email": "notif-submitter@example.com",
			}
		).insert(ignore_permissions=True)

		self.addCleanup(self._purge)

	def _ensure_area(self):
		"""A leaf area. Grievance refuses to attach to a group node."""
		if not frappe.db.exists("Submitter Type", "Individual Farmer"):
			frappe.get_doc(
				{"doctype": "Submitter Type", "type_name": "Individual Farmer", "code": "IND"}
			).insert(ignore_permissions=True)

		root = frappe.db.get_value("Administrative Area", {"area_name": "Notif Root"}, "name")
		if not root:
			root = (
				frappe.get_doc(
					{
						"doctype": "Administrative Area",
						"area_name": "Notif Root",
						"level_name": "Country",
						"code": "NFR",
						"is_group": 1,
					}
				)
				.insert(ignore_permissions=True)
				.name
			)

		leaf = frappe.db.get_value("Administrative Area", {"area_name": "Notif Woreda"}, "name")
		if not leaf:
			leaf = (
				frappe.get_doc(
					{
						"doctype": "Administrative Area",
						"area_name": "Notif Woreda",
						"level_name": "Woreda",
						"code": "NFW",
						"parent_administrative_area": root,
						"is_group": 0,
					}
				)
				.insert(ignore_permissions=True)
				.name
			)
		return leaf

	def _ensure_grievance_type(self):
		existing = frappe.db.get_value("Grievance Type", {"type_name": "Notif Test Type"}, "name")
		if existing:
			return existing
		return (
			frappe.get_doc(
				{
					"doctype": "Grievance Type",
					"type_name": "Notif Test Type",
					"service_category": "Inputs",
				}
			)
			.insert(ignore_permissions=True)
			.name
		)

	def _purge(self):
		for name in frappe.get_all(
			"Grievance Notification Log", filters={"grievance": self.grievance.name}, pluck="name"
		):
			frappe.db.delete("Grievance Notification Log", name)
		for name in frappe.get_all("Notification", filters={"method": EVENT}, pluck="name"):
			frappe.delete_doc("Notification", name, force=True, ignore_permissions=True)

	def _make_notification(self, recipient_role, channel="Email", enabled=1, condition=None):
		doc = frappe.get_doc(
			{
				"doctype": "Notification",
				"name": f"Grievance: Test {recipient_role} ({channel})",
				"subject": '{{ _("Test") }}',
				"document_type": "Grievance",
				"event": "Method",
				"method": EVENT,
				"channel": channel,
				"grievance_recipient": recipient_role,
				"message": '{{ _("Grievance {0} test", context="grievance.test").format(doc.name) }}',
				"message_type": "Plain Text",
				"is_standard": 0,
				"enabled": enabled,
				"condition": condition,
			}
		).insert(ignore_permissions=True)
		return doc

	def _rows(self):
		return frappe.get_all(
			"Grievance Notification Log",
			filters={"grievance": self.grievance.name, "event": EVENT},
			fields=["name", "recipient", "channel", "status", "language", "message"],
		)

	def test_queues_exactly_one_row_and_does_not_duplicate_on_refire(self):
		self._make_notification(notifications.RECIPIENT_DEPARTMENT_HEAD)

		notifications.queue(self.grievance, EVENT)
		self.assertEqual(len(self._rows()), 1)

		# Appendix C's control against redundant messaging.
		notifications.queue(self.grievance, EVENT)
		rows = self._rows()
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0].recipient, self.head)
		self.assertEqual(rows[0].status, "Queued")

	def test_disabled_notification_produces_no_row(self):
		self._make_notification(notifications.RECIPIENT_DEPARTMENT_HEAD, enabled=0)
		notifications.queue(self.grievance, EVENT)
		self.assertEqual(self._rows(), [])

	def test_false_condition_produces_no_row(self):
		self._make_notification(notifications.RECIPIENT_DEPARTMENT_HEAD, condition='doc.status == "Closed"')
		notifications.queue(self.grievance, EVENT)
		self.assertEqual(self._rows(), [])

	def test_department_roles_resolve_through_the_link_hop(self):
		"""Core cannot follow assigned_dept -> Grievance Department -> head_of_dept."""
		cases = {
			notifications.RECIPIENT_DEPARTMENT_HEAD: self.head,
			notifications.RECIPIENT_NODAL_OFFICER: self.nodal,
			notifications.RECIPIENT_TOP_LEVEL: self.senior,
			notifications.RECIPIENT_DEPARTMENT_OFFICER: "notif-dept@example.com",
		}
		for role, expected in cases.items():
			with self.subTest(role=role):
				self.assertEqual(notifications.resolve_recipient(self.grievance, role), expected)

	def test_sms_row_is_evidenced_in_the_log(self):
		"""Core's SMS Log is unlinked, success-only and bypassed by a send_sms hook."""
		self._make_notification(notifications.RECIPIENT_DEPARTMENT_HEAD, channel="SMS")
		notifications.queue(self.grievance, EVENT)

		rows = self._rows()
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0].channel, "SMS")
		self.assertEqual(rows[0].recipient, self.head)
		self.assertIn(self.grievance.name, rows[0].message)

	def test_both_channels_of_one_event_each_get_a_row(self):
		"""FSD "SMS + Email" is two Notification records, because channel is a Select.

		Regression guard: with the dedupe key on (grievance, event, recipient) alone,
		whichever of the pair was queued second was silently dropped, so every
		"SMS + Email" row in Appendix C delivered on one channel only.
		"""
		self._make_notification(notifications.RECIPIENT_DEPARTMENT_HEAD, channel="Email")
		self._make_notification(notifications.RECIPIENT_DEPARTMENT_HEAD, channel="SMS")

		notifications.queue(self.grievance, EVENT)
		self.assertEqual({row.channel for row in self._rows()}, {"Email", "SMS"})

		# Re-firing still must not duplicate either channel.
		notifications.queue(self.grievance, EVENT)
		self.assertEqual(len(self._rows()), 2)

	def test_log_row_cannot_be_deleted(self):
		self._make_notification(notifications.RECIPIENT_DEPARTMENT_HEAD)
		notifications.queue(self.grievance, EVENT)
		row = self._rows()[0]

		# Administrator bypasses DocPerms, so the controller guard is what must hold.
		self.assertRaises(
			frappe.ValidationError,
			frappe.delete_doc,
			"Grievance Notification Log",
			row.name,
			ignore_permissions=True,
		)

	def test_no_role_holds_delete_permission(self):
		meta = frappe.get_meta("Grievance Notification Log")
		self.assertTrue(meta.permissions)
		for perm in meta.permissions:
			with self.subTest(role=perm.role):
				self.assertFalse(perm.delete)


class TestNotificationTranslatability(FrappeTestCase):
	"""The validate hook that keeps admin-edited wording translatable."""

	def tearDown(self):
		for name in frappe.get_all("Notification", filters={"method": "translatability_probe"}, pluck="name"):
			frappe.delete_doc("Notification", name, force=True, ignore_permissions=True)

	def _save(self, message):
		return frappe.get_doc(
			{
				"doctype": "Notification",
				"name": f"Grievance: Translatability {frappe.generate_hash(length=6)}",
				"subject": '{{ _("Test") }}',
				"document_type": "Grievance",
				"event": "Method",
				"method": "translatability_probe",
				"channel": "Email",
				"message": message,
				"message_type": "Plain Text",
				"is_standard": 0,
				"enabled": 1,
			}
		).insert(ignore_permissions=True)

	def test_literal_text_is_rejected(self):
		self.assertRaises(frappe.ValidationError, self._save, "Your grievance has been received.")

	def test_wrapped_text_is_accepted(self):
		doc = self._save('{{ _("Your grievance {0} received").format(doc.name) }}')
		self.assertTrue(doc.name)

	def test_non_grievance_notifications_are_untouched(self):
		doc = frappe.get_doc(
			{
				"doctype": "Notification",
				"name": f"Unrelated {frappe.generate_hash(length=6)}",
				"subject": "Test",
				"document_type": "ToDo",
				"event": "New",
				"channel": "Email",
				"message": "Plain English is fine here.",
				"is_standard": 0,
				"enabled": 0,
			}
		).insert(ignore_permissions=True)
		self.addCleanup(frappe.delete_doc, "Notification", doc.name, force=True)
		self.assertTrue(doc.name)
