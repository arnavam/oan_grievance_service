# Copyright (c) 2026, COSS - Centre for Open Societal Systems and Contributors
# See license.txt

import frappe
from frappe.tests.utils import FrappeTestCase

from oan_grievance_service.grievance_masters.doctype.grievance_administrative_area import (
	grievance_administrative_area as area_module,
)
from oan_grievance_service.services import constants as C
from oan_grievance_service.tests.fixtures import a_free_ticket_code, an_administrative_area


class TestAdministrativeAreaTicketCode(FrappeTestCase):
	"""The ticket character is checked on save, not when a farmer files.

	A bad code is otherwise invisible until the first submission in that region
	fails, and a duplicate never fails at all — it quietly gives two regions the
	same ticket prefix.
	"""

	def _region(self, name, ticket_code):
		existing = frappe.db.get_value("Grievance Administrative Area", {"area_name": name}, "name")
		if existing:
			doc = frappe.get_doc("Grievance Administrative Area", existing)
			doc.ticket_code = ticket_code
			return doc
		return frappe.get_doc(
			{
				"doctype": "Grievance Administrative Area",
				"area_name": name,
				"level_name": "Region",
				"code": name.replace(" ", "")[:8].upper(),
				"ticket_code": ticket_code,
				"is_group": 1,
			}
		)

	def test_valid_single_character_is_accepted_and_uppercased(self):
		doc = self._region("Code Valid Region", "k")
		doc.insert(ignore_permissions=True)
		self.assertEqual(doc.ticket_code, "K")

	def test_excluded_characters_are_refused(self):
		for char in "ILOU":
			with self.assertRaises(frappe.ValidationError, msg=f"{char} should be refused"):
				self._region(f"Code Excluded {char}", char).insert(ignore_permissions=True)

	def test_wrong_width_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			self._region("Code Too Long Region", "AB").insert(ignore_permissions=True)

	def test_duplicate_region_code_is_refused(self):
		self._region("Code First Region", "Q").insert(ignore_permissions=True)
		with self.assertRaises(frappe.ValidationError):
			self._region("Code Second Region", "Q").insert(ignore_permissions=True)

	def test_only_regions_carry_a_ticket_code(self):
		parent = self._region("Code Parent Region", "W")
		parent.insert(ignore_permissions=True)
		with self.assertRaises(frappe.ValidationError):
			frappe.get_doc(
				{
					"doctype": "Grievance Administrative Area",
					"area_name": "Code Child Woreda",
					"level_name": "Woreda",
					"code": "CCW",
					"ticket_code": "X",
					"parent_administrative_area": parent.name,
					"is_group": 0,
				}
			).insert(ignore_permissions=True)

	def test_a_region_without_a_code_still_saves(self):
		"""Most of the tree has none; only regions used for filing need one."""
		doc = self._region("Code Absent Region", None)
		doc.insert(ignore_permissions=True)
		self.assertFalse(doc.ticket_code)


class TestAdministrativeArea(FrappeTestCase):
	def test_tree_structure_and_lft_rgt(self):
		root_name = frappe.db.get_value(
			"Grievance Administrative Area", {"area_name": "Test Country"}, "name"
		)
		if not root_name:
			root = frappe.get_doc(
				{
					"doctype": "Grievance Administrative Area",
					"area_name": "Test Country",
					"level_name": "Country",
					"code": "TCT",
					"is_group": 1,
				}
			).insert(ignore_permissions=True)
		else:
			root = frappe.get_doc("Grievance Administrative Area", root_name)

		child1_name = frappe.db.get_value(
			"Grievance Administrative Area", {"area_name": "Test Region 1"}, "name"
		)
		if not child1_name:
			child1 = frappe.get_doc(
				{
					"doctype": "Grievance Administrative Area",
					"area_name": "Test Region 1",
					"level_name": "Region",
					"code": "TR1",
					"parent_administrative_area": root.name,
					"is_group": 1,
				}
			).insert(ignore_permissions=True)
		else:
			child1 = frappe.get_doc("Grievance Administrative Area", child1_name)

		child2_name = frappe.db.get_value(
			"Grievance Administrative Area", {"area_name": "Test Woreda 1"}, "name"
		)
		if not child2_name:
			child2 = frappe.get_doc(
				{
					"doctype": "Grievance Administrative Area",
					"area_name": "Test Woreda 1",
					"level_name": "Woreda",
					"code": "TW1",
					"parent_administrative_area": child1.name,
					"is_group": 0,
				}
			).insert(ignore_permissions=True)
		else:
			child2 = frappe.get_doc("Grievance Administrative Area", child2_name)

		root.reload()
		child1.reload()
		child2.reload()

		# Containment checks
		self.assertLess(root.lft, child1.lft)
		self.assertGreater(root.rgt, child1.rgt)
		self.assertLess(child1.lft, child2.lft)
		self.assertGreater(child1.rgt, child2.rgt)

	def test_get_areas_endpoint(self):
		from oan_grievance_service.api.v1.administrative_area import get_areas

		suffix = frappe.generate_hash(length=6)
		created = []

		def make_area(name, parent, level_name, **overrides):
			doc_name = an_administrative_area(f"{name} {suffix}", parent, level_name, **overrides)
			created.append(doc_name)
			return doc_name, frappe.db.get_value("Grievance Administrative Area", doc_name, "code")

		self.addCleanup(
			lambda: [
				frappe.delete_doc("Grievance Administrative Area", n, force=True, ignore_permissions=True)
				for n in reversed(created)
			]
		)

		country, _ = make_area("Areas Country", None, C.AREA_LEVEL_COUNTRY)
		r1, r1_code = make_area(
			"Areas Region 1", country, C.AREA_LEVEL_REGION, ticket_code=a_free_ticket_code()
		)
		r2, r2_code = make_area(
			"Areas Region 2", country, C.AREA_LEVEL_REGION, ticket_code=a_free_ticket_code()
		)
		z1, _ = make_area("Areas Zone 1", r1, C.AREA_LEVEL_ZONE)
		make_area("Areas Zone 2", r2, C.AREA_LEVEL_ZONE)
		make_area("Areas Woreda 1", z1, C.AREA_LEVEL_WOREDA)

		# Default root view returns regions
		res = get_areas()
		self.assertIn("data", res)
		self.assertIn("areas", res["data"])
		self.assertGreaterEqual(res["data"]["count"], 1)

		# Cascading drilldown with single parent (by external code)
		res_child = get_areas(parent=r1_code)
		self.assertIn("data", res_child)
		self.assertGreaterEqual(res_child["data"]["count"], 1)
		for area in res_child["data"]["areas"]:
			self.assertEqual(area["parent_administrative_area"], r1_code)

		# Multi-parent drilldown as list
		res_multi_list = get_areas(parent=[r1_code, r2_code])
		self.assertIn("data", res_multi_list)
		self.assertGreaterEqual(res_multi_list["data"]["count"], 1)
		for area in res_multi_list["data"]["areas"]:
			self.assertIn(area["parent_administrative_area"], [r1_code, r2_code])

		# Multi-parent drilldown as comma-separated string
		res_multi_csv = get_areas(parent=f"{r1_code}, {r2_code}")
		self.assertIn("data", res_multi_csv)
		self.assertGreaterEqual(res_multi_csv["data"]["count"], 1)
		for area in res_multi_csv["data"]["areas"]:
			self.assertIn(area["parent_administrative_area"], [r1_code, r2_code])

		# Multi-parent drilldown with level_name filter (subtree intervals)
		res_multi_tier = get_areas(parent=[r1_code, r2_code], level_name="Woreda")
		self.assertIn("data", res_multi_tier)
		for area in res_multi_tier["data"]["areas"]:
			self.assertEqual(area["level_name"], "Woreda")

		# Single level_name filtering
		res_level = get_areas(level_name="Zone")
		self.assertIn("data", res_level)
		for area in res_level["data"]["areas"]:
			self.assertEqual(area["level_name"], "Zone")

		# Ancestor breadcrumbs lookup (by external code)
		res_ancestors = get_areas(ancestors_of=r1_code)
		self.assertIn("data", res_ancestors)
		self.assertIn("breadcrumbs", res_ancestors["data"])
		self.assertGreaterEqual(len(res_ancestors["data"]["breadcrumbs"]), 1)


class TestRollupLookups(FrappeTestCase):
	"""How the dashboard rollups identify areas: Region by code, Woreda by code or else name."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		suffix = frappe.generate_hash(length=6)
		cls.areas = []

		def area(name, parent, level_name, **overrides):
			created = an_administrative_area(f"{name} {suffix}", parent, level_name, **overrides)
			cls.areas.append(created)
			return created

		cls.country = area("Lookup Country", None, C.AREA_LEVEL_COUNTRY)
		cls.region = area("Lookup Region", cls.country, C.AREA_LEVEL_REGION, ticket_code=a_free_ticket_code())
		cls.coded = area("Lookup Coded", cls.region, C.AREA_LEVEL_WOREDA)
		cls.uncoded = area("Lookup Uncoded", cls.region, C.AREA_LEVEL_WOREDA, code=None)
		cls.suffix = suffix

	@classmethod
	def tearDownClass(cls):
		for name in reversed(cls.areas):
			frappe.delete_doc("Grievance Administrative Area", name, force=True, ignore_permissions=True)
		super().tearDownClass()

	def _row(self, name):
		return frappe.db.get_value(
			"Grievance Administrative Area", name, ["name", "lft", "rgt", "code"], as_dict=True
		)

	def test_a_woreda_is_keyed_by_code_or_else_name_and_a_region_by_code_only(self):
		self.assertEqual(area_module.rollup_key(C.AREA_LEVEL_WOREDA, "W1", "Name"), "W1")
		self.assertEqual(area_module.rollup_key(C.AREA_LEVEL_WOREDA, None, "Name"), "Name")
		self.assertEqual(area_module.rollup_key(C.AREA_LEVEL_REGION, "R1", "Name"), "R1")
		self.assertIsNone(area_module.rollup_key(C.AREA_LEVEL_REGION, None, "Name"))

	def test_the_keys_within_an_area_include_itself_and_every_woreda_below(self):
		region = self._row(self.region)
		keys = area_module.rollup_keys_within(C.AREA_LEVEL_WOREDA, region)
		self.assertEqual(set(keys), {self._row(self.coded).code, self.uncoded})
		self.assertEqual(
			area_module.rollup_keys_within(C.AREA_LEVEL_REGION, region), (self._row(self.region).code,)
		)
		self.assertEqual(area_module.rollup_keys_within(C.AREA_LEVEL_REGION, self._row(self.coded)), ())

	def test_names_come_back_for_the_requested_keys_only(self):
		code = self._row(self.coded).code
		names = area_module.area_names(C.AREA_LEVEL_WOREDA, [code, self.uncoded])
		self.assertEqual(
			names, {code: f"Lookup Coded {self.suffix}", self.uncoded: f"Lookup Uncoded {self.suffix}"}
		)
		self.assertEqual(
			area_module.area_names(C.AREA_LEVEL_WOREDA, [code]), {code: f"Lookup Coded {self.suffix}"}
		)
		self.assertEqual(area_module.area_names(C.AREA_LEVEL_WOREDA, []), {})

	def test_scope_areas_carry_the_level_and_agree_with_area_bounds(self):
		scopes = [{"administrative_area_scope": self.region}, {"administrative_area_scope": None}]
		areas = area_module.scope_areas(scopes)
		self.assertEqual(set(areas), {self.region})
		self.assertEqual(areas[self.region].level_name, C.AREA_LEVEL_REGION)
		region = self._row(self.region)
		self.assertEqual(area_module.area_bounds(scopes), {self.region: (region.lft, region.rgt)})
		self.assertEqual(area_module.scope_areas([]), {})

	def test_the_country_is_the_first_country_area(self):
		country = area_module.country_area()
		self.assertIsNotNone(country)
		first = frappe.get_all(
			"Grievance Administrative Area",
			filters={"level_name": C.AREA_LEVEL_COUNTRY},
			fields=["code", "area_name"],
			order_by="lft asc",
			limit=1,
		)[0]
		self.assertEqual(country, first)
