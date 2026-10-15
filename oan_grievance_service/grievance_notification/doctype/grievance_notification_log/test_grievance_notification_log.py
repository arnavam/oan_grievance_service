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

# sms_log is a Link to core's SMS Log, which exists on version-16 but not on
# version-15 -- there sms_settings.py guards its own writes with
# `if not frappe.db.exists("DocType", "SMS Log")`. The test runner walks a doctype's
# Link fields to build test records and throws DoesNotExistError when the target is
# absent, stopping the whole suite before a single test runs. Kept so the suite runs
# on either branch; the field is read-only and populated only opportunistically, so
# skipping the dependency costs nothing.
test_ignore = ["SMS Log"]


def _ensure_user(email, mobile=None, language=None):
	"""An officer login: a desk refuses a user who does not hold the officer role."""
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
	frappe.get_doc("User", email).add_roles("Grievance Officer")
	return email


class NotificationCase(FrappeTestCase):
	"""A department with three officers, a leaf area, and one grievance to notify about."""

	def setUp(self):
		from oan_grievance_service.setup.install import seed_role_levels

		seed_role_levels()

		self.head = _ensure_user("notif-head@example.com", "+251900000001")
		self.nodal = _ensure_user("notif-nodal@example.com", "+251900000002")
		self.senior = _ensure_user("notif-senior@example.com", "+251900000003")

		dept_name = frappe.db.get_value("Grievance Department", {"dept_name": "Notif Test Dept"}, "name")
		if not dept_name:
			self.dept = frappe.get_doc(
				{
					"doctype": "Grievance Department",
					"dept_name": "Notif Test Dept",
					"short_name": "NTD",
				}
			).insert(ignore_permissions=True)
		else:
			self.dept = frappe.get_doc("Grievance Department", dept_name)

		if not frappe.db.exists("Grievance RBAC Assignment", {"department_scope": self.dept.name}):
			self.assignment = frappe.get_doc(
				{
					"doctype": "Grievance RBAC Assignment",
					"department_scope": self.dept.name,
					"active": 1,
					"effective_from": frappe.utils.today(),
					"officers": [
						{
							"user": self.head,
							"role_level": "department_head",
							"is_primary": 1,
							"active": 1,
						},
						{
							"user": self.nodal,
							"role_level": "nodal_officer",
							"is_primary": 1,
							"active": 1,
						},
						{
							"user": self.senior,
							"role_level": "senior_nodal_officer",
							"is_primary": 1,
							"active": 1,
						},
					],
				}
			).insert(ignore_permissions=True)

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
		from oan_grievance_service.tests.fixtures import a_leaf_area

		return a_leaf_area()

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
		for name in frappe.get_all(
			"Grievance RBAC Assignment", filters={"department_scope": self.dept.name}, pluck="name"
		):
			frappe.delete_doc("Grievance RBAC Assignment", name, force=True, ignore_permissions=True)

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


class TestGrievanceNotificationLog(NotificationCase):
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
			notifications.RECIPIENT_DEPARTMENT_OFFICER: self.head,
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


SYNTHETIC_USER = "notif-synthetic@id.openagrinet.internal"
LOGIN_EMAIL = "notif-login@example.com"
CASE_EMAIL = "notif-submitter@example.com"
PROFILE_EMAIL = "notif-profile@example.com"


class TestDeliveryAddresses(NotificationCase):
	"""Where a queued row actually goes.

	The auth service registers every User under a synthetic @id.openagrinet.internal
	name and keeps the real address in `oan_login_email`, because core's User.validate
	writes `name` back into `email` on every save. The old send path read `User.email`,
	so every acknowledgement email went to an address no mail server delivers, while
	the farmer's real address sat unused on the profile.

	Precedence, agreed on PR #26 review: for a registered submitter the registered
	contact always wins (profile, then User). The contact snapshot on the case is
	used only for a submitter with no account, since for a registered farmer it is a
	filing-time copy of the profile and can only lag it.
	"""

	def setUp(self):
		super().setUp()
		if not frappe.db.exists("User", SYNTHETIC_USER):
			frappe.get_doc(
				{
					"doctype": "User",
					"email": SYNTHETIC_USER,
					"first_name": "Synthetic",
					"send_welcome_email": 0,
					"mobile_no": "+251900000009",
				}
			).insert(ignore_permissions=True)
		self.has_login_email = frappe.db.has_column("User", "oan_login_email")
		if self.has_login_email:
			frappe.db.set_value("User", SYNTHETIC_USER, "oan_login_email", LOGIN_EMAIL)

		profile = frappe.get_doc(
			{
				"doctype": "Grievance Submitter Profile",
				"submitter_type": "Individual Farmer",
				"submitter_name": "Notif Synthetic Farmer",
				"contact_mobile": "+251922222222",
				"contact_email": PROFILE_EMAIL,
				"user": SYNTHETIC_USER,
			}
		).insert(ignore_permissions=True)
		self.addCleanup(frappe.delete_doc, "Grievance Submitter Profile", profile.name, force=True)
		self.profile = profile

		frappe.db.set_value(
			"Grievance",
			self.grievance.name,
			{"submitter": profile.name, "contact_email": CASE_EMAIL, "contact_mobile": "+251911111111"},
		)
		self.grievance.reload()

	def _case_contacts(self, email=None, mobile=None):
		frappe.db.set_value(
			"Grievance", self.grievance.name, {"contact_email": email, "contact_mobile": mobile}
		)
		self.grievance.reload()

	def test_the_submitter_row_resolves_to_their_user(self):
		recipient = notifications.resolve_recipient(self.grievance, notifications.RECIPIENT_SUBMITTER)
		self.assertEqual(recipient, SYNTHETIC_USER)

	def test_registered_email_wins_over_the_case_snapshot(self):
		# The case still says CASE_EMAIL; the profile is the contact of record.
		self.assertEqual(notifications._deliverable_email(SYNTHETIC_USER, self.grievance), PROFILE_EMAIL)

	def test_login_email_is_the_fallback_when_the_profile_has_none(self):
		frappe.db.set_value("Grievance Submitter Profile", self.profile.name, "contact_email", None)
		if not self.has_login_email:
			self.skipTest("oan_login_email is the auth service's field; not installed here")
		self.assertEqual(notifications._deliverable_email(SYNTHETIC_USER, self.grievance), LOGIN_EMAIL)

	def test_the_case_snapshot_is_not_used_for_a_registered_submitter(self):
		# Registered contact missing everywhere: the stale snapshot must not step in.
		frappe.db.set_value("Grievance Submitter Profile", self.profile.name, "contact_email", None)
		if self.has_login_email:
			frappe.db.set_value("User", SYNTHETIC_USER, "oan_login_email", None)
		with self.assertRaises(ValueError):
			notifications._deliverable_email(SYNTHETIC_USER, self.grievance)

	def test_the_synthetic_address_is_never_used(self):
		frappe.db.set_value("Grievance Submitter Profile", self.profile.name, "contact_email", None)
		if self.has_login_email:
			frappe.db.set_value("User", SYNTHETIC_USER, "oan_login_email", None)
		# Only User.email is left, and it is the synthetic registration name.
		with self.assertRaises(ValueError):
			notifications._deliverable_email(SYNTHETIC_USER, self.grievance)

	def test_a_staff_user_with_a_real_email_keeps_it(self):
		# Officers created from the desk carry their address in User.email itself.
		self.assertEqual(notifications._deliverable_email(self.head, self.grievance), self.head)

	def test_a_submitter_without_an_account_gets_the_case_snapshot(self):
		# Walk-in or IVR: staff filed the case, no profile User. resolve_recipient hands
		# back the contact snapshot and that is the only address there is.
		frappe.db.set_value("Grievance Submitter Profile", self.profile.name, "user", None)
		recipient = notifications.resolve_recipient(self.grievance, notifications.RECIPIENT_SUBMITTER)
		self.assertEqual(recipient, "+251911111111")
		self.assertEqual(notifications._deliverable_email(recipient, self.grievance), CASE_EMAIL)
		self.assertEqual(notifications._deliverable_mobile(recipient, self.grievance), "+251911111111")

	def test_a_bare_recipient_falls_back_to_itself_when_the_case_has_no_email(self):
		# No account and no email on the case: the bare address the row carries is all
		# there is. With an email on the case, the snapshot wins (previous test).
		frappe.db.set_value("Grievance Submitter Profile", self.profile.name, "user", None)
		frappe.db.set_value("Grievance", self.grievance.name, "contact_email", None)
		self.grievance.reload()
		self.assertEqual(
			notifications._deliverable_email("walkin@example.com", self.grievance), "walkin@example.com"
		)

	def test_registered_mobile_wins_over_the_case_snapshot(self):
		self.assertEqual(notifications._deliverable_mobile(SYNTHETIC_USER, self.grievance), "+251922222222")

	def test_user_mobile_is_the_sms_fallback_not_the_case(self):
		frappe.db.set_value("Grievance Submitter Profile", self.profile.name, "contact_mobile", "")
		self.assertEqual(notifications._deliverable_mobile(SYNTHETIC_USER, self.grievance), "+251900000009")
		frappe.db.set_value("User", SYNTHETIC_USER, "mobile_no", None)
		with self.assertRaises(ValueError):
			notifications._deliverable_mobile(SYNTHETIC_USER, self.grievance)


class TestSmsFailsClosed(NotificationCase):
	"""An SMS row is Sent only when a gateway actually took the message.

	Core's `_send_sms` msgprints and returns when SMS Settings has no gateway URL, and
	writes an SMS Log only on a 2xx from the gateway. Found on the dev bench: with no
	gateway configured at all, every SMS row was marked Sent.
	"""

	def setUp(self):
		super().setUp()
		if frappe.get_hooks("send_sms"):
			self.skipTest("a send_sms hook owns delivery here; core's SMS Log is bypassed")
		self._gateway_was = frappe.db.get_single_value("SMS Settings", "sms_gateway_url")
		self.addCleanup(self._set_gateway, self._gateway_was)
		self._real_send = notifications._send_sms
		self.addCleanup(setattr, notifications, "_send_sms", self._real_send)
		self.row = frappe._dict(recipient=self.head, message="Ticket test", grievance=self.grievance.name)

	def _set_gateway(self, url):
		frappe.db.set_single_value("SMS Settings", "sms_gateway_url", url)

	def _fake_gateway(self, writes_log):
		from frappe.core.doctype.sms_settings.sms_settings import create_sms_log

		calls = []

		def fake(receiver_list, msg, sender_name="", success_msg=True):
			calls.append(receiver_list)
			if writes_log:
				create_sms_log(
					{"receiver_list": receiver_list, "message": msg.encode("utf-8")}, receiver_list
				)

		notifications._send_sms = fake
		return calls

	def test_no_gateway_configured_fails_the_row(self):
		self._set_gateway(None)
		calls = self._fake_gateway(writes_log=True)
		with self.assertRaises(ValueError) as caught:
			notifications._send_sms_row(self.row, self.grievance)
		self.assertIn("No SMS gateway", str(caught.exception))
		self.assertEqual(calls, [], "nothing should be handed to core without a gateway")

	def test_a_gateway_that_confirms_nothing_fails_the_row(self):
		self._set_gateway("http://gateway.test/send")
		self._fake_gateway(writes_log=False)
		with self.assertRaises(ValueError) as caught:
			notifications._send_sms_row(self.row, self.grievance)
		self.assertIn("did not confirm", str(caught.exception))

	def test_a_confirmed_send_links_the_sms_log(self):
		if not frappe.db.exists("DocType", "SMS Log"):
			self.skipTest("SMS Log doctype not present on this Frappe version")
		self._set_gateway("http://gateway.test/send")
		calls = self._fake_gateway(writes_log=True)
		result = notifications._send_sms_row(self.row, self.grievance)
		self.assertTrue(result.get("sms_log"))
		self.assertEqual(calls, [["+251900000001"]])

	def test_dispatch_records_failed_not_sent_without_a_gateway(self):
		self._set_gateway(None)
		self._fake_gateway(writes_log=True)
		self._make_notification(notifications.RECIPIENT_DEPARTMENT_HEAD, channel="SMS")
		notifications.queue(self.grievance, EVENT)

		notifications.dispatch_queued(grievance=self.grievance.name)

		row = self._rows()[0]
		self.assertEqual(row.status, "Failed")


class TestAcknowledgementTemplate(FrappeTestCase):
	"""The seeded Submission Received wording, with and without an SLA due date.

	The SLA clock starts at assignment, so a case no routing rule matched has no due
	date when the acknowledgement goes out. The template used to print the field
	directly, which reached a farmer as "Expected response by None".
	"""

	def _template(self):
		from oan_grievance_service.services import constants as C
		from oan_grievance_service.setup.install import NOTIFICATION_EVENTS, _translatable

		for code, _title, _role, _channels, _trigger, source, args in NOTIFICATION_EVENTS:
			if code == C.EVENT_SUBMISSION_RECEIVED:
				return _translatable(source, args, f"grievance.{code}")
		self.fail("Submission Received row missing from NOTIFICATION_EVENTS")

	def _render(self, **doc):
		context = {"doc": frappe._dict(ticket_number="B001000J0", service_category="Inputs", **doc)}
		return frappe.render_template(self._template(), context)

	def test_it_passes_the_translatability_guard(self):
		doc = frappe.get_doc(
			{
				"doctype": "Notification",
				"document_type": "Grievance",
				"channel": "SMS",
				"subject": '{{ _("Submission Received") }}',
				"message": self._template(),
			}
		)
		notifications.validate_notification(doc)  # must not raise

	def test_a_routed_case_shows_the_due_date(self):
		text = self._render(sla_due_date="2026-09-25 10:00:00")
		self.assertIn("B001000J0", text)
		self.assertIn("Expected response by 2026-09-25", text)
		self.assertNotIn("None", text)

	def test_an_unrouted_case_gets_the_fallback(self):
		text = self._render(sla_due_date=None)
		self.assertIn("received under Inputs.", text)
		self.assertIn("You will be informed once an officer is assigned.", text)
		self.assertNotIn("None", text)


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
