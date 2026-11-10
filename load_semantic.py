"""Build the semantic layer: the database `semantic` (the shard fibovg.semantic), a graph of what the lake's tables,
columns and codes mean in FIBO.

Usage (run.sh does this): uv run --with neo4j --with pyyaml python load_semantic.py

Three sources, one graph:
  physical  the lake's tables and columns, read from Trino's information_schema      (:Table)-[:HAS_COLUMN]->(:Column)
  graph     VG's mapping of tables to labels and properties (vg/home/schema.json)    (:NodeMap) (:PropertyMap) (:RelMap)
  semantic  what those mean in FIBO (semantic/mapping.yaml)                          -[:MAPS_TO]-> (:Concept {iri})

Every name in mapping.yaml is checked against the other two, and every IRI against the FIBO database. A name or IRI
that does not resolve fails the load. An IRI that FIBO only imports (OMG Commons, LCC: in the graph, but without a
label) is accepted with a warning. The database is rebuilt from scratch on each run.
"""

import csv
import io
import json
import subprocess
import sys
from pathlib import Path

import neo4j
import yaml

HERE = Path(__file__).parent
PASSWORD = (HERE / ".state/neo4j-password").read_text().strip()
VG_SCHEMA = json.loads((HERE / "vg/home/schema.json").read_text())
MAPPING = yaml.safe_load((HERE / "semantic/mapping.yaml").read_text())
CATALOG, SCHEMA = VG_SCHEMA["catalog"], VG_SCHEMA["schema"]

errors: list[str] = []
warnings: list[str] = []


def trino_columns() -> dict[str, list[tuple[str, str]]]:
    """table -> [(column, type)], from the lake's own catalog."""
    sql = (
        f"SELECT table_name, column_name, data_type FROM {CATALOG}.information_schema.columns "
        f"WHERE table_schema = '{SCHEMA}' ORDER BY table_name, ordinal_position"
    )
    out = subprocess.run(
        ["docker", "exec", "fibo-vg-trino", "trino", "--output-format", "CSV", "--execute", sql],
        capture_output=True, text=True, check=True,
    ).stdout
    tables: dict[str, list[tuple[str, str]]] = {}
    for table, column, dtype in csv.reader(io.StringIO(out)):
        tables.setdefault(table, []).append((column, dtype))
    return tables


def expand(curie: str) -> str:
    prefix, sep, local = curie.partition(":")
    if sep and prefix in MAPPING["prefixes"]:
        return MAPPING["prefixes"][prefix] + local
    if curie.startswith(("http://", "https://")):
        return curie
    errors.append(f"unknown prefix in {curie!r}")
    return curie


def as_rule(v) -> dict:  # a class rule's value is a CURIE, or {class, note}
    return {"class": v, "note": None} if isinstance(v, str) else {"class": v["class"], "note": v.get("note")}


physical = trino_columns()
vg_nodes = {n["label"]: n for n in VG_SCHEMA["entities"]["nodes"]}
vg_rels = {r["label"]: r for r in VG_SCHEMA["entities"]["relationships"]}


def column(table: str, name: str, where: str) -> str:
    if name not in {c for c, _ in physical.get(table, [])}:
        errors.append(f"{where}: no column {table}.{name} in the lake")
    return f"{CATALOG}.{SCHEMA}.{table}.{name}"


driver = neo4j.GraphDatabase.driver("bolt://localhost:7690", auth=("neo4j", PASSWORD), notifications_min_severity="OFF")
driver.execute_query("CREATE DATABASE semantic IF NOT EXISTS WAIT", database_="system")

# Concepts: every IRI the mapping uses, looked up in FIBO.
iris: set[str] = set()
for label, m in MAPPING.get("nodes", {}).items():
    iris.add(expand(m["class"])) if m.get("class") else None
    for v in (m.get("classRules") or {}).get("values", {}).values():
        iris.add(expand(as_rule(v)["class"]))
    for p in (m.get("properties") or {}).values():
        iris.update(expand(p[k]) for k in ("property", "valueClass") if p.get(k))
for rtype, m in MAPPING.get("relationships", {}).items():
    iris.update(expand(m[k]) for k in ("property", "startRole", "endRole") if m.get(k))
    for p in (m.get("properties") or {}).values():
        iris.update(expand(p[k]) for k in ("property", "valueClass") if p.get(k))
found = {
    r["uri"]: r
    for r in driver.execute_query(
        "MATCH (r:Resource) WHERE r.uri IN $iris "
        "RETURN r.uri AS uri, r.label AS label, CASE WHEN r:Class THEN 'class' "
        "WHEN r:ObjectProperty OR r:DatatypeProperty THEN 'property' ELSE 'resource' END AS kind",
        iris=sorted(iris), database_="fibo",
    ).records
}
concepts = []
for iri in sorted(iris):
    if iri not in found:
        errors.append(f"IRI not in FIBO: {iri}")
        continue
    if found[iri]["label"] is None:
        warnings.append(f"imported by FIBO but not loaded (no label): {iri}")
    concepts.append({"iri": iri, "label": found[iri]["label"], "kind": found[iri]["kind"], "external": found[iri]["label"] is None})

# Physical layer
tables = [
    {"fqn": f"{CATALOG}.{SCHEMA}.{t}", "name": t,
     "columns": [{"fqn": f"{CATALOG}.{SCHEMA}.{t}.{c}", "name": c, "dataType": d} for c, d in cols]}
    for t, cols in physical.items()
]

# Graph layer (VG) + semantic links
node_maps, rule_rows, rel_maps = [], [], []
for label, vg in vg_nodes.items():
    m = MAPPING.get("nodes", {}).get(label)
    if m is None:
        warnings.append(f"VG label {label} has no semantic mapping")
        m = {}
    props = {p["name"]: p for p in vg["properties"]}
    for name in m.get("properties") or {}:
        if name not in props:
            errors.append(f"nodes.{label}.properties.{name}: VG's {label} has no property {name}")
    rules = m.get("classRules") or {}
    if rules and rules["column"] not in {p["column"] for p in vg["properties"]}:
        errors.append(f"nodes.{label}.classRules: column {rules['column']} is not a VG property, so rows cannot be filtered on it")
    node_maps.append({
        "label": label,
        "table": f"{CATALOG}.{SCHEMA}.{vg['table']}",
        "class": expand(m["class"]) if m.get("class") else None,
        "keys": [column(vg["table"], k["column"], f"VG {label} key") for k in vg["key"]],
        "properties": [
            {"name": name,
             "column": column(vg["table"], p["column"], f"VG {label}.{name}"),
             "property": expand(sm["property"]) if (sm := (m.get("properties") or {}).get(name, {})).get("property") else None,
             "valueClass": expand(sm["valueClass"]) if sm.get("valueClass") else None,
             "note": sm.get("note")}
            for name, p in props.items()
        ],
    })
    for value, v in (rules.get("values") or {}).items():
        r = as_rule(v)
        rule_rows.append({"label": label, "column": column(vg["table"], rules["column"], f"nodes.{label}.classRules"),
                          "property": next(p["name"] for p in vg["properties"] if p["column"] == rules["column"]),
                          "value": value, "class": expand(r["class"]), "note": r["note"]})
for missing in set(MAPPING.get("nodes", {})) - set(vg_nodes):
    errors.append(f"nodes.{missing}: VG has no label {missing}")

for rtype, vg in vg_rels.items():
    m = MAPPING.get("relationships", {}).get(rtype)
    if m is None:
        warnings.append(f"VG relationship {rtype} has no semantic mapping")
        m = {}
    props = {p["name"]: p for p in vg.get("properties", [])}
    rel_maps.append({
        "type": rtype,
        "table": f"{CATALOG}.{SCHEMA}.{vg['table']}",
        "start": vg["start"]["targetEntity"],
        "end": vg["end"]["targetEntity"],
        "startKeys": [column(vg["table"], k["relationshipColumn"], f"VG {rtype} start") for k in vg["start"]["keys"]],
        "endKeys": [column(vg["table"], k["relationshipColumn"], f"VG {rtype} end") for k in vg["end"]["keys"]],
        "property": expand(m["property"]) if m.get("property") else None,
        "inverse": bool(m.get("inverse", False)),
        "startRole": expand(m["startRole"]) if m.get("startRole") else None,
        "endRole": expand(m["endRole"]) if m.get("endRole") else None,
        "note": m.get("note"),
        "properties": [
            {"name": name,
             "column": column(vg["table"], p["column"], f"VG {rtype}.{name}"),
             "property": expand(sm["property"]) if (sm := (m.get("properties") or {}).get(name, {})).get("property") else None,
             "valueClass": expand(sm["valueClass"]) if sm.get("valueClass") else None,
             "note": sm.get("note")}
            for name, p in props.items()
        ],
    })
for missing in set(MAPPING.get("relationships", {})) - set(vg_rels):
    errors.append(f"relationships.{missing}: VG has no relationship {missing}")

for w in warnings:
    print(f"  warning: {w}", file=sys.stderr)
if errors:
    for e in errors:
        print(f"  error: {e}", file=sys.stderr)
    sys.exit(f"fibo-vg: semantic layer not loaded ({len(errors)} errors)")

PROPERTY_MAPS = """
  WITH owner, pm
  CREATE (owner)-[:HAS_PROPERTY]->(p:PropertyMap {owner: coalesce(owner.label, owner.type), name: pm.name, note: pm.note})
  WITH p, pm
  MATCH (c:Column {fqn: pm.column})
  CREATE (p)-[:FROM_COLUMN]->(c)
  WITH p, pm
  OPTIONAL MATCH (prop:Concept {iri: pm.property})
  FOREACH (_ IN CASE WHEN prop IS NULL THEN [] ELSE [1] END | CREATE (p)-[:MAPS_TO]->(prop))
  WITH p, pm
  OPTIONAL MATCH (vc:Concept {iri: pm.valueClass})
  FOREACH (_ IN CASE WHEN vc IS NULL THEN [] ELSE [1] END | CREATE (p)-[:VALUE_CLASS]->(vc))
"""

with driver.session(database="semantic") as s:
    s.run("MATCH (n) DETACH DELETE n").consume()
    for label, prop in [("Table", "fqn"), ("Column", "fqn"), ("Concept", "iri"), ("NodeMap", "label"), ("RelMap", "type")]:
        s.run(f"CREATE CONSTRAINT {label.lower()}_{prop} IF NOT EXISTS FOR (n:{label}) REQUIRE n.{prop} IS UNIQUE").consume()
    s.run("UNWIND $concepts AS c CREATE (:Concept {iri: c.iri, label: c.label, kind: c.kind, external: c.external})",
          concepts=concepts).consume()
    s.run("""
      UNWIND $tables AS t
      CREATE (tbl:Table {fqn: t.fqn, name: t.name})
      WITH tbl, t UNWIND t.columns AS c
      CREATE (tbl)-[:HAS_COLUMN]->(:Column {fqn: c.fqn, name: c.name, dataType: c.dataType, table: t.name})
    """, tables=tables).consume()
    s.run("""
      UNWIND $maps AS m
      MATCH (t:Table {fqn: m.table})
      CREATE (n:NodeMap {label: m.label})-[:FROM_TABLE]->(t)
      WITH n, m
      OPTIONAL MATCH (cls:Concept {iri: m.class})
      FOREACH (_ IN CASE WHEN cls IS NULL THEN [] ELSE [1] END | CREATE (n)-[:MAPS_TO]->(cls))
      WITH n, m
      CALL (n, m) { UNWIND m.keys AS k MATCH (c:Column {fqn: k}) CREATE (n)-[:KEY]->(c) }
      WITH n AS owner, m UNWIND m.properties AS pm
    """ + PROPERTY_MAPS, maps=node_maps).consume()
    s.run("""
      UNWIND $rules AS r
      MATCH (n:NodeMap {label: r.label}), (c:Column {fqn: r.column}), (k:Concept {iri: r.class})
      CREATE (n)-[:HAS_RULE]->(rule:ClassRule {property: r.property, value: r.value, note: r.note})-[:ASSIGNS]->(k),
             (rule)-[:ON_COLUMN]->(c)
    """, rules=rule_rows).consume()
    s.run("""
      UNWIND $maps AS m
      MATCH (t:Table {fqn: m.table}), (a:NodeMap {label: m.start}), (b:NodeMap {label: m.end})
      CREATE (r:RelMap {type: m.type, inverse: m.inverse, note: m.note})-[:FROM_TABLE]->(t), (r)-[:START]->(a), (r)-[:END]->(b)
      WITH r, m
      CALL (r, m) { UNWIND m.startKeys AS k MATCH (c:Column {fqn: k}) CREATE (r)-[:START_KEY]->(c) }
      CALL (r, m) { UNWIND m.endKeys AS k MATCH (c:Column {fqn: k}) CREATE (r)-[:END_KEY]->(c) }
      CALL (r, m) {
        OPTIONAL MATCH (p:Concept {iri: m.property})
        OPTIONAL MATCH (sr:Concept {iri: m.startRole})
        OPTIONAL MATCH (er:Concept {iri: m.endRole})
        FOREACH (_ IN CASE WHEN p IS NULL THEN [] ELSE [1] END | CREATE (r)-[:MAPS_TO {inverse: m.inverse}]->(p))
        FOREACH (_ IN CASE WHEN sr IS NULL THEN [] ELSE [1] END | CREATE (r)-[:START_ROLE]->(sr))
        FOREACH (_ IN CASE WHEN er IS NULL THEN [] ELSE [1] END | CREATE (r)-[:END_ROLE]->(er))
      }
      WITH r AS owner, m UNWIND m.properties AS pm
    """ + PROPERTY_MAPS, maps=rel_maps).consume()

    cov = s.run("""
      MATCH (c:Column)
      WITH c, EXISTS { (c)<-[:FROM_COLUMN]-(:PropertyMap)-[:MAPS_TO|VALUE_CLASS]->() } AS meaning,
              EXISTS { (c)<-[:ON_COLUMN]-(:ClassRule) } OR EXISTS { (c)<-[:START_KEY|END_KEY]-(:RelMap) } AS structural
      RETURN count(c) AS columns, count(CASE WHEN meaning THEN 1 END) AS meaning,
             collect(CASE WHEN NOT meaning AND NOT structural THEN c.table + '.' + c.name END) AS unmapped
    """).single()
driver.close()
print(f"fibo-vg: semantic layer: {len(tables)} tables, {cov['columns']} columns ({cov['meaning']} with a FIBO meaning), "
      f"{len(node_maps)} labels, {len(rule_rows)} class rules, {len(rel_maps)} relationships, {len(concepts)} FIBO concepts"
      + (f"; columns with no role: {', '.join(cov['unmapped'])}" if cov["unmapped"] else ""))
