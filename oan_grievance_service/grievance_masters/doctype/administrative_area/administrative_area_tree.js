frappe.treeview_settings["Administrative Area"] = {
	breadcrumbs: "Grievance Management",
	title: __("Administrative Area Tree"),
	get_tree_nodes: "frappe.desk.treeview.get_children",
	add_tree_node: "frappe.desk.treeview.add_node",
	get_tree_root: false,
	root_label: "Ethiopia",
	filters: [
		{
			fieldname: "is_active",
			fieldtype: "Check",
			label: __("Active"),
			default: 1,
		},
	],
	fields: [
		{ fieldtype: "Data", fieldname: "area_name", label: __("Area Name"), reqd: true },
		{ fieldtype: "Data", fieldname: "code", label: __("Code") },
		{
			fieldtype: "Select",
			fieldname: "area_type",
			label: __("Area Type"),
			options: "Country\nRegion\nZone\nWoreda\nKebele\nVillage\nOther",
			default: "Region",
			reqd: true,
		},
		{ fieldtype: "Check", fieldname: "is_group", label: __("Is Group") },
	],
};
