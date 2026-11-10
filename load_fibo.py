"""Load FIBO's production ontology (the unzipped prod.ttl.zip) into the Neo4j database `fibo` with neosemantics.

Usage (run.sh does this): uv run --with neo4j python load_fibo.py fibo/ttl

Each module's Turtle file is sent inline (n10s.rdf.import.inline), so the server needs no file access. The graph config
drops namespace prefixes (handleVocabUris IGNORE), so FIBO reads as (:Class {uri, label, definition})-[:subClassOf]->(:Class).
FIBO's own owl:imports of OMG Commons and LCC are outside the zip and not loaded: those IRIs appear as bare :Resource nodes.
"""

import sys
from pathlib import Path

import neo4j

HERE = Path(__file__).parent
PASSWORD = (HERE / ".state/neo4j-password").read_text().strip()
ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else HERE / "fibo/ttl")

GRAPH_CONFIG = {
    "handleVocabUris": "IGNORE",  # rdfs:label -> label, skos:definition -> definition, owl:Class -> :Class
    "handleMultival": "OVERWRITE",
    "keepLangTag": False,
    "handleRDFTypes": "LABELS",
}

driver = neo4j.GraphDatabase.driver(
    "bolt://localhost:7690", auth=("neo4j", PASSWORD), notifications_min_severity="OFF"
)
with driver.session(database="fibo") as s:
    s.run("CREATE CONSTRAINT n10s_unique_uri IF NOT EXISTS FOR (r:Resource) REQUIRE r.uri IS UNIQUE").consume()
    if s.run("MATCH (c:_GraphConfig) RETURN count(c) AS n").single()["n"] == 0:
        s.run("CALL n10s.graphconfig.init($c)", c=GRAPH_CONFIG).consume()

    files = sorted(ROOT.rglob("*.ttl"))
    if not files:
        sys.exit(f"no .ttl files under {ROOT}")
    total = 0
    for f in files:
        r = s.run(
            "CALL n10s.rdf.import.inline($rdf, 'Turtle', {languageFilter: 'en'}) "
            "YIELD terminationStatus, triplesLoaded, extraInfo RETURN *",
            rdf=f.read_text(encoding="utf-8"),
        ).single()
        total += r["triplesLoaded"]
        if r["terminationStatus"] != "OK":
            print(f"  {f.relative_to(ROOT)}: {r['terminationStatus']} {r['extraInfo']}", file=sys.stderr)
    counts = s.run(
        "MATCH (c:Class) WITH count(c) AS classes "
        "MATCH ()-[r:subClassOf]->() RETURN classes, count(r) AS subClassOf"
    ).single()
    print(f"fibo-vg: {len(files)} files, {total} triples; {counts['classes']} classes, {counts['subClassOf']} subClassOf edges")
driver.close()
