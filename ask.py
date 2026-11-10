"""Ask the lake a question in FIBO's terms: a class, and optionally the properties wanted. The semantic layer decides
which label, which rows and which columns that means; this script writes the Virtual Graph query and runs it.

Usage (or ./run.sh ask ...):
  uv run --with neo4j python ask.py "collective investment vehicle"
  uv run --with neo4j python ask.py "depository institution" "has legal name" "is incorporated in"
  uv run --with neo4j python ask.py fibo-fbc-acct:BrokerageAccount "has balance"

A class or property is named by its FIBO label, its IRI, or a prefix:name from semantic/mapping.yaml.
Every step goes through the composite database `fibovg`, one shard at a time:
  1. fibovg.ontology  the class and its subclasses; each property and its subproperties
  2. fibovg.semantic  which labels map to those classes (whole table, or rows by a code), and which properties to those properties
  3. fibovg.lake      the generated query, on VG

This is the translation step that cannot be done inside one Cypher query: a lake subquery in the composite cannot take
variables from another shard, and VG does not accept dynamic labels or property names.
"""

import sys
from pathlib import Path

import neo4j

HERE = Path(__file__).parent
PASSWORD = (HERE / ".state/neo4j-password").read_text().strip()
PREFIXES = {}
for line in (HERE / "semantic/mapping.yaml").read_text().split("nodes:")[0].splitlines():  # just the prefixes block
    k, sep, v = line.strip().partition(": ")
    if sep and v.startswith("http"):
        PREFIXES[k] = v

if len(sys.argv) < 2:
    sys.exit(__doc__)
CLASS_TERM, PROPERTY_TERMS = sys.argv[1], sys.argv[2:]
driver = neo4j.GraphDatabase.driver("bolt://localhost:7690", auth=("neo4j", PASSWORD), notifications_min_severity="OFF")


def q(cypher: str, **params) -> list[dict]:
    return [r.data() for r in driver.execute_query(cypher, params, database_="fibovg").records]


def iri_of(term: str) -> str | None:
    prefix, sep, local = term.partition(":")
    if sep and prefix in PREFIXES:
        return PREFIXES[prefix] + local
    return term if term.startswith(("http://", "https://")) else None


def resolve(term: str, kind: str, down: str) -> tuple[dict, list[str]]:
    """The term's node in FIBO, and the IRIs of it and everything below it (subclasses or subproperties)."""
    match = "(t:Class)" if kind == "class" else "(t) WHERE (t:ObjectProperty OR t:DatatypeProperty)"
    rows = q(
        f"USE fibovg.ontology MATCH {match} "
        + ("AND " if kind != "class" else "WHERE ")
        + "(t.uri = $iri OR toLower(t.label) = toLower($term)) "
        f"OPTIONAL MATCH (t)<-[:{down}*0..]-(sub) "
        "RETURN t.uri AS iri, t.label AS label, collect(DISTINCT sub.uri) AS below",
        iri=iri_of(term), term=term,
    )
    if not rows:
        sys.exit(f"no FIBO {kind} called {term!r}")
    if len(rows) > 1:
        sys.exit(f"{term!r} names {len(rows)} FIBO {kind}es; use the IRI: " + ", ".join(r["iri"] for r in rows))
    return rows[0], rows[0]["below"] or [rows[0]["iri"]]


cls, classes = resolve(CLASS_TERM, "class", "subClassOf")
print(f"class      {cls['label']}  <{cls['iri']}>  ({len(classes)} with its subclasses)")
props = [resolve(t, "property", "subPropertyOf") for t in PROPERTY_TERMS]

# Which labels hold instances: all rows, if the table's class is one of them; else the rows whose code maps to one.
targets = q(
    """
    USE fibovg.semantic
    MATCH (n:NodeMap)-[:FROM_TABLE]->(t:Table)
    OPTIONAL MATCH (n)-[:MAPS_TO]->(whole:Concept) WHERE whole.iri IN $classes
    OPTIONAL MATCH (n)-[:HAS_RULE]->(r:ClassRule)-[:ASSIGNS]->(k:Concept) WHERE k.iri IN $classes
    WITH n, t, whole, collect(r {.property, .value, class: k.label}) AS rules
    WHERE whole IS NOT NULL OR size(rules) > 0
    MATCH (n)-[:HAS_PROPERTY]->(p:PropertyMap)-[:FROM_COLUMN]->(c:Column)
    OPTIONAL MATCH (p)-[:MAPS_TO]->(pc:Concept)
    OPTIONAL MATCH (p)-[:VALUE_CLASS]->(vc:Concept)
    WITH n, t, whole, rules, collect(p {.name, column: c.name, iri: pc.iri, meaning: coalesce(pc.label, vc.label, p.name),
                                           key: EXISTS { (n)-[:KEY]->(c) }}) AS properties
    OPTIONAL MATCH (n)-[:HAS_RULE]->(all:ClassRule)-[:ASSIGNS]->(ak:Concept)
    RETURN n.label AS label, t.fqn AS table, whole IS NOT NULL AS allRows, rules, properties,
           collect(all {.value, class: ak.label}) AS codes, all.property AS codeProperty
    """,
    classes=classes,
)
if not targets:
    sys.exit(f"nothing in the lake is mapped to {cls['label']} or its subclasses")

for t in targets:
    # columns: the requested FIBO properties, or every mapped one; always the key-like columns first
    if props:
        cols = [m for m in t["properties"] if m["key"]]  # the row's identifier, always
        for (p, below) in props:
            hit = [m for m in t["properties"] if m["iri"] in below]
            if not hit:
                print(f"           ({t['label']} has no column mapped to {p['label']})")
            cols += [m for m in hit if m not in cols]
    else:
        cols = [m for m in t["properties"] if m["key"] or m["iri"] or m["meaning"] != m["name"]]
    codes = {c["value"]: c["class"] for c in t["codes"] if c["value"] is not None}
    where, params = "", {}
    if not t["allRows"]:
        # one equality per code, ORed: VG's default SQL dialect does not support IN
        values = sorted(r["value"] for r in t["rules"])
        params = {f"code{i}": v for i, v in enumerate(values)}
        where = " WHERE " + " OR ".join(f"n.`{t['rules'][0]['property']}` = ${k}" for k in params)
    returns = [f"n.`{m['name']}` AS `{m['meaning']}`" for m in cols]
    if t["codeProperty"]:
        returns.append(f"n.`{t['codeProperty']}` AS code")
    cypher = f"USE fibovg.lake MATCH (n:`{t['label']}`){where} RETURN " + ", ".join(returns)

    print(f"\nlabel      {t['label']}  (table {t['table']})")
    print("rows       " + ("all: the table's class is " + cls["label"] + " or below" if t["allRows"] else
                           "by code: " + ", ".join(f"{r['value']} = {r['class']}" for r in t["rules"])))
    print("columns    " + ", ".join(f"{m['column']} = {m['meaning']}" for m in cols))
    print(f"query      {cypher}" + (f"   {params}" if params else ""))
    rows = q(cypher, **params)
    headers = [m["meaning"] for m in cols] + (["FIBO class (from the code)"] if t["codeProperty"] else [])
    table = [[("" if r[m["meaning"]] is None else str(r[m["meaning"]])) for m in cols]
             + ([codes.get(r["code"], f"{r['code']} (unmapped code)")] if t["codeProperty"] else []) for r in rows]
    widths = [max(len(h), *(len(row[i]) for row in table)) if table else len(h) for i, h in enumerate(headers)]
    print()
    print("  " + "  ".join(h.ljust(w) for h, w in zip(headers, widths)))
    print("  " + "  ".join("-" * w for w in widths))
    for row in table:
        print("  " + "  ".join(v.ljust(w) for v, w in zip(row, widths)))
    print(f"  ({len(table)} rows)")
driver.close()
