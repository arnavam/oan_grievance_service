# Administrative Area Tree

The hierarchical geography model for the Grievance Service. A single nested-set tree replaces the `regions` and `woredas` tables in `database-schema.md` §1, so a deployment in a new country is a data load rather than a migration.

This is a schema specification, in the same terms as `database-schema.md`: PostgreSQL types, no implementation code. Where the Frappe layer above it matters, it is called out as a note, not as the design.

---

## 1. Problem

Administrative divisions differ in both naming and depth:

- **Ethiopia** — Country → Region → Zone → Woreda → Kebele (5 levels)
- **Kenya** — Country → County → Sub-County → Ward → Village (5 levels)
- **Nigeria** — Country → State → Local Government Area → Ward (4 levels)
- **India** — Country → State → District → Taluka → Village (5 levels)

A table per tier hardcodes one country's constitution into the schema. `woredas.region_id` is not a fact about geography, it is a fact about Ethiopia, and it is the column that has to be dropped to deploy anywhere else.

The depth also varies _within_ a country. Ethiopia's chartered cities (Addis Ababa, Dire Dawa) sit directly under the federal level with sub-cities in place of zones, so even the Ethiopia-only design already has a ragged tree it was pretending not to have.

---

## 2. Shape of the tree

One tree, one root. The root is a synthetic **World** node; each country is a child of it.

```
World  (root, is_group)
├── Ethiopia            level 1 — Country
│   └── Oromia          level 2 — Region
│       └── East Shewa  level 3 — Zone
│           └── Adama   level 4 — Woreda
│               ├── Kebele 01   level 5 — Kebele (leaf)
│               └── Kebele 02   level 5 — Kebele (leaf)
└── Kenya               level 1 — Country
    └── Nakuru          level 2 — County
        └── Naivasha    level 3 — Sub-County
            └── Biashara level 4 — Ward (leaf)
```

**Why a synthetic root rather than one root per country.** A nested set is a single interval space: `lft`/`rgt` are only comparable across nodes that share a tree. Per-country roots give you _n_ disjoint trees in one table, which means every range query needs a `country_id` predicate alongside the range to stay correct, and any tooling that assumes one root — including Frappe's `NestedSet`, whose `validate_one_root` throws `NestedSetMultipleRootsError` on the second root — breaks on the second country. One root costs one row and removes both problems. Nothing is ever attached to the World node; it exists to make the interval space total.

**Depth is data, not schema.** `depth` and `level_name` describe a node; no constraint anywhere requires a particular number of levels, and nothing computes a level by counting joins.

---

## 3. `administrative_areas`

Replaces `regions` and `woredas`. Reference data with an effective-dated history, per `database-schema.md` §1 — Ethiopia's regions have split twice in a decade and will again.

| Column             | Type        | Key                                | Description                                                                               |
| ------------------ | ----------- | ---------------------------------- | ----------------------------------------------------------------------------------------- |
| `id`               | uuid        | PK                                 | UUIDv7, per the standing decision                                                         |
| `parent_id`        | uuid        | FK → `administrative_areas.id`, IX | Null only for the World root                                                              |
| `country_id`       | uuid        | FK → `administrative_areas.id`, IX | The level-1 ancestor, denormalised. Null on the root                                      |
| `code`             | varchar(16) | UQ¹                                | Short code used in ticket IDs — `OROM`, `BISH`. Unique among siblings                     |
| `path_code`        | text        | UQ                                 | Materialised dotted code path — `ET.OROM.ESHW.ADAM.K01`. The stable external identifier   |
| `name`             | text        |                                    | Display name — "Adama". **Not unique**: "Kebele 01" recurs in every woreda in the country |
| `level_name`       | text        |                                    | Label for the tier — Region, Woreda, Kebele, County, Ward                                 |
| `depth`            | smallint    | IX                                 | 0 = World, 1 = Country. Denormalised for level-filtered queries                           |
| `is_group`         | boolean     |                                    | False marks an operational leaf a grievance may attach to                                 |
| `lft`              | int         | IX                                 | Nested set left index                                                                     |
| `rgt`              | int         | IX                                 | Nested set right index                                                                    |
| `valid_from`       | date        |                                    | When this division came into existence                                                    |
| `valid_to`         | date        | IX²                                | Null while current. Set, never deleted, when a division is dissolved or split             |
| `superseded_by_id` | uuid        | FK → `administrative_areas.id`     | Where this area's territory went on a split or merge                                      |
| `created_at`       | timestamptz |                                    |                                                                                           |
| `updated_at`       | timestamptz |                                    |                                                                                           |

¹ Unique per `(parent_id, code)`.
² Partial index `WHERE valid_to IS NULL` — every operational query wants current areas only.

**`name` is deliberately not unique.** Naming a node by its display name is the first thing that fails: `Kebele 01` exists roughly a thousand times in Ethiopia. The identity of a node is its `id`; its human-readable identity is `path_code`, which is unique because it carries its ancestry.

**`code` is unique among siblings, not globally.** Two woredas in different zones may both be `BISH`. `path_code` disambiguates.

**Indexes.** Separate B-tree indexes on `lft` and on `rgt` — not a composite `(lft, rgt)`. Containment predicates constrain the two columns independently, and a composite index can only serve the leading one.

---

## 4. Querying the tree

Three questions get asked, and each is a single indexed query.

**Everything under an area** — the officer's queue, the regional dashboard, subtree permission scoping:

```
WHERE lft > :area_lft AND rgt < :area_rgt
```

**The ancestor chain of an area** — breadcrumbs, and nearest-rule resolution:

```
WHERE lft < :area_lft AND rgt > :area_rgt
ORDER BY lft DESC
```

**The nearest ancestor carrying a routing rule** — one query, not a walk:

```
FROM category_assignments ca
JOIN administrative_areas a ON a.id = ca.administrative_area_id
WHERE a.lft <= :case_lft AND a.rgt >= :case_rgt
  AND ca.category_id = :category_id
ORDER BY (a.rgt - a.lft) ASC
LIMIT 1
```

The subtree with the smallest span is the deepest matching ancestor, so most-specific-wins falls out of the ordering. This replaces climbing the tree one level at a time, which cost one round trip per level on the submission hot path.

### Denormalise `lft` onto the grievance

`grievances` carries `area_lft` alongside `administrative_area_id`, indexed.

One column suffices: a grievance sits at a single leaf, and testing whether that leaf falls inside an officer's subtree needs only the leaf's `lft` against the officer's interval (§6). The grievance's `rgt` would carry no information the `lft` does not.

Without it, every scoped list query joins to `administrative_areas` before it can filter, so the join drives the plan and the covering indexes in `database-schema.md` §_The read path_ never get used. With it, an officer's queue is one index range scan on one table.

The copy is safe because the source is near-static: `lft` changes only when an area moves, which is a governance event measured in years. A move triggers a bounded backfill over the affected subtree, and a nightly reconciliation job checks for drift the way the response-counter job does.

### Cost of a write

Inserting into a nested set renumbers every node to the right of the insertion point — `UPDATE ... SET lft = lft + 2 WHERE lft > :x`. That is the standard objection to nested sets, and here it does not apply: the tree is reference data of order 10⁵ nodes that changes on a legislative timescale, while reads run on every request. The trade is exactly the right way round.

The one place it bites is bulk load. Loading a country row by row is O(n²) because each insert renumbers the rows already loaded. Country fixtures are loaded with `lft`/`rgt` precomputed offline and inserted in one pass, then verified — never inserted individually.

---

## 5. Attaching a grievance to an area

A grievance attaches to exactly one area, and it must be a leaf (`is_group = false`) that is current (`valid_to IS NULL`). Both conditions are enforced in the database, not in the form: a `CHECK` cannot see the referenced row, so this is a trigger on insert and update of `grievances.administrative_area_id`.

A UI filter that offers only leaf nodes is a convenience. It is not the enforcement, because the API is reachable without it.

### Snapshot the hierarchy at submission

`grievances` stores `area_path_code` — the resolved dotted path — as a plain text column, written once at submission and never updated.

The live tree answers "where is this case now"; the snapshot answers "where was it filed". When a woreda is split, every historical case under it silently re-attributes to the new hierarchy unless the path was captured, and last year's SLA compliance report changes the next time it is run. The snapshot is the only thing that makes prior-period reporting reproducible.

This is a deliberate exception to _derivable is not stored_: it is not derivable, because the tree it was derived from no longer exists.

---

## 6. Scoping officer visibility

An officer's `administrative_area_id`, held on their profile record, scopes what they can see. Setting it to a region grants the whole region; setting it to the country node grants national visibility. The check is interval containment:

```
WHERE g.area_lft > :officer_lft AND g.area_lft < :officer_rgt
```

**One column on the grievance, not two.** Nested set intervals nest strictly — they never partially overlap — so a node lies inside subtree _X_ exactly when `X.lft < node.lft < X.rgt`. The grievance's `rgt` adds nothing to the test. The officer's own `lft`/`rgt` are read from their profile and cached for the session; they change only when the officer's posting changes.

One range predicate against one index, independent of whether the subtree holds ten areas or a hundred thousand.

### What this costs to build

Scoping is **custom work**, and the estimate should say so. Frappe's native row-level mechanism is the `User Permission` DocType, and it cannot express a range: for a nested-set doctype it materialises the full descendant list (`user_permission.py:136-139`), caches it per user, and inlines it as escaped string literals into an `IN (...)` clause on every list query (`db_query.py:1145-1147`). For a national officer that is every area in the country, re-parsed per request. The interval predicate above is not reachable through it.

Four pieces, none optional:

| Piece                                     | Why                                                                                                                                                                                                                                                                                            |
| ----------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `permission_query_conditions` hook        | Emits the interval predicate. Reads the officer's area from the profile record — no `User Permission` rows are involved anywhere in this design.                                                                                                                                               |
| `has_permission` hook                     | The query hook fires on list queries only. Without a matching document-level check, `get_doc` on any case ID reads across jurisdictions.                                                                                                                                                       |
| `area_lft` denormalised onto `grievances` | Keeps the predicate on the table already being scanned. Without it the hook must join to `administrative_areas`, and the join drives the plan.                                                                                                                                                 |
| CI guard on `get_all`                     | `frappe.get_all` / `frappe.db.get_all` / `frappe.db.get_list` bypass both hooks. The sibling `oan_a2c` app AST-scans every Python file in CI and fails the build on an unscoped call against a tenant-scoped doctype; area scoping carries the identical hazard and needs the identical guard. |

Set `ignore_user_permissions: 1` on the grievance's link field to `Administrative Area`. Not because this design creates `User Permission` rows — it does not — but because an administrator can create one from the desk UI without a developer involved, and `add_user_permissions` would then inject its `IN` clause _on top of_ the hook. The flag makes that impossible rather than merely unlikely.

### Index shape is an `EXPLAIN` question

The officer queue filters on state and the area range, then sorts by activity. A range predicate on the leading column prevents later columns serving the sort, so `(area_lft, state_id, last_activity_at DESC)` range-scans then sorts — fine for a woreda officer whose range is narrow, less so for a national officer whose range is the whole table. `(state_id, area_lft)` inverts the trade. Which wins depends on real cardinality and on the actual mix of narrow-scope and national officers, so it is settled against a production-sized tree, not here.

---

## 7. Country fixtures

One fixture per country, versioned and checksummed, loaded by a seed job rather than by hand.

| Field        | Notes                                                            |
| ------------ | ---------------------------------------------------------------- |
| `path_code`  | The full dotted path — identifies the row and implies its parent |
| `code`       | Segment code, unique among siblings                              |
| `name`       | Display name                                                     |
| `level_name` | Tier label                                                       |
| `is_group`   | False for operational leaves                                     |
| `valid_from` | Effective date of the division                                   |

`parent_id` is not in the fixture: it is derived from `path_code`, so a fixture cannot express a row whose parent it does not also contain. `lft`/`rgt` are computed in one pass after load, and the load is rejected if the resulting tree has more than one root, any orphan, or any cycle.

Ethiopia ships first. Kenya, Nigeria and India are fixtures against the same schema, with no code path that names a country.

---

## 8. Read APIs

Three endpoints, all paginated. None of them returns a tree.

| Endpoint                                         | Returns                                                  |
| ------------------------------------------------ | -------------------------------------------------------- |
| `GET /administrative-areas/children?parent=…`    | Direct children of one node — the cascading dropdown     |
| `GET /administrative-areas/{id}/ancestors`       | Ordered ancestor chain — breadcrumbs                     |
| `GET /administrative-areas/search?q=…&country=…` | Prefix search over `name` and `path_code`, leaf-filtered |

There is no "return the full tree" endpoint. A country is ~10⁵ nodes; serialising it is a multi-megabyte response that no client needs, because every real interface either descends one level at a time or searches. The tree is served lazily or not at all.

Responses are cached with the tree's version stamp as the key, and invalidated on any write to `administrative_areas`. Writes are rare; the cache hit rate approaches one.

---

## 9. Verification

1. Interval integrity after load, move and dissolve: one root, no orphans, no cycles, `lft < rgt` for every node, and no interval overlap between siblings.
2. Bulk fixture load for Ethiopia and Kenya coexisting in one tree, verifying that the second country's load does not renumber into the first's range incorrectly.
3. `depth`, `country_id` and `path_code` agree with the structural parentage they denormalise, checked by the reconciliation job.
4. `grievances.area_lft` agrees with the referenced area after a subtree move.
5. Rejection of a grievance attached to a group node or to an area with `valid_to` set.
6. Scoping: an officer at one node sees every case in their subtree and no case outside it — asserted at each depth, including the country node and a leaf.
7. Query plans for the officer queue and the nearest-rule lookup are index range scans, asserted against a tree loaded to production cardinality.
