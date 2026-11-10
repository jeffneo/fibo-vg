# FIBO + Iceberg in one composite database

Scaffolding for this architecture: the **FIBO ontology** loaded into Neo4j with **neosemantics (n10s)**, an **Iceberg data lake** served as a graph by a self-managed **Neo4j Virtual Graph (VG)**, and both of them used as **shards of one composite database**. Local Docker only; nothing is published beyond `127.0.0.1`.

No RBAC and no credential pass-through: Trino has no authentication, and VG reads it as a single user. For that, see `neovg-trino-iceberg`, which this is adapted from.

```bash
./run.sh up      # object store, Iceberg catalog, Trino (seeded), VG, Neo4j with FIBO, the composite database
./run.sh check   # the example queries in cypher/examples.cypher, on the composite database
./run.sh shell   # cypher-shell on the composite database
./run.sh fibo    # reload FIBO (up does it when the database is empty)
./run.sh down    # remove the containers and their data (downloads and .state/ stay)
```

Needs Docker, `uv`, `keytool` (any JDK), `openssl`, `curl`, `unzip`, `zip`. On the first run, `run.sh` downloads Trino's JDBC driver (checked against Maven Central's SHA-1), n10s, and FIBO's production release.

## What it is

```
                                   graph: Neo4j 5.26 Enterprise  (bolt://localhost:7690)
                                   ┌──────────────────────────────────────────────────────┐
  Cypher ──▶ composite  fibovg ──┬─▶ fibovg.ontology ─▶ database fibo (FIBO via n10s)     │
                                 │                                                        │
                                 └─▶ fibovg.lake ──── remote alias, neo4j+ssc://vg:7687 ──┼──┐
                                   └──────────────────────────────────────────────────────┘  │
                                                                                             ▼
  vg: Neo4j 2026.09 Enterprise, Virtual Graph (bolt://localhost:7691) ── jdbc:trino ──▶ Trino 484 (http://localhost:8081)
                                                                                         │ REST
                                                                                         ▼
                                                                    Iceberg REST catalog ──▶ S3Mock (Parquet files)
```

| Shard | What | How |
| --- | --- | --- |
| `fibovg.ontology` | FIBO production (156 modules, about 134k triples, 2,544 classes) | `load_fibo.py` sends each module's Turtle to `n10s.rdf.import.inline`. The graph config is `handleVocabUris: IGNORE`, so classes read as `(:Class {uri, label, definition})-[:subClassOf]->(:Class)`. |
| `fibovg.lake` | 3 Iceberg tables in `iceberg.lake`: `legal_entities` (12), `accounts` (10), `ownership` (5) | VG's `generic` JDBC data source on Trino ([vg/home/](vg/home/)). The graph is `(:LegalEntity)-[:HOLDS]->(:Account)-[:SERVICED_BY]->(:LegalEntity)` and `(:LegalEntity)-[:OWNS {pct}]->(:LegalEntity)`. |

**The join between the shards:** every lake row carries `fibo_class`, the IRI of the FIBO class it belongs to (for example, `…/SEC/Funds/Funds/HedgeFund`). It matches `Class.uri` in the ontology, so a query can read the lake and ask FIBO what each row *is*.

## Example queries ([cypher/examples.cypher](cypher/examples.cypher))

1. **Lake only:** who holds which account, and who services it.
2. **Ontology only:** FIBO's superclasses of "hedge fund" (open-end investment → managed investment → collective investment vehicle → …).
3. **Both:** each lake entity with its FIBO class label and definition.
4. **Classified by the ontology:** balances held by *any kind of fund*. "Fund" means a subclass of collective investment vehicle in FIBO, not a list in the query. The result is the ETF, the mutual fund and the hedge fund. The pension fund drops out, because FIBO does not make a pension fund a collective investment vehicle.
5. **Classified by the ontology:** accounts serviced by a depository institution (the commercial bank and the credit union, but not the broker-dealer).

The pattern for queries across both shards:

```cypher
CALL () { USE fibovg.lake      MATCH (e:LegalEntity) RETURN e.name AS entity, e.fibo_class AS cls }
CALL (cls) { USE fibovg.ontology MATCH (c:Class {uri: cls}) RETURN c.label AS fiboClass }
RETURN entity, fiboClass
```

## What I found while building it (2026-11-09)

1. **n10s does not run on Neo4j 2026.09.** No n10s release lists 2026.x in its `versions.json`, so `NEO4J_PLUGINS='["n10s"]'` cannot install it. The latest jar (2025.06.1) loads only in part: 2026.09's procedure classloader fails on n10s's package-private nested classes (`IllegalAccessError`), and `n10s.graphconfig.*`, `n10s.nsprefixes.*`, `n10s.add.*` and the mapping procedures are missing. Without `graphconfig.init`, nothing can be imported. Writing the config node by hand does not work either: n10s reads its settings as Java `Integer`, and Cypher stores `Long`. **The graph instance is therefore Neo4j 5.26 LTS with n10s 5.26.0**, and VG stays on 2026.09. The composite reaches VG over bolt, and mixing the two versions worked.
2. **n10s 5.26.0 stops Neo4j 5.26.31 from starting** (`NoSuchMethodError: JsonProperty.isRequired`). It is a fat jar with an old, unshaded `jackson-annotations`, and the image puts `/plugins/*` ahead of Neo4j's `lib/*` on the classpath. `run.sh` mounts a copy with `com/fasterxml/jackson/*` removed (`plugins/neosemantics-5.26.0-nojackson.jar`). After that, all 55 n10s procedures load. The only remaining load warnings are for optional HTTP-cache classes (memcached, ehcache).
3. **A remote alias has to use TLS.** Neo4j 5.26 refuses `neo4j://` for `CREATE ALIAS … AT` and asks for `neo4j+s` or `neo4j+ssc`. VG's bolt has a self-signed certificate (`.state/vg-bolt/`) at `tls_level: OPTIONAL`, and the alias uses `neo4j+ssc` (encrypted, certificate not verified). With a real certificate, use `neo4j+s`. A remote alias also needs a keystore for the stored password (`.state/aliases.p12`, settings in `docker-compose.yml`).
4. **VG requires a `secret.json`** even when Trino has no authentication. A `basic` secret with an empty password works. The user then has to come from the secret and not the JDBC URL, or the driver refuses: `user is passed both by URL and properties`.
5. **A lake subquery cannot import variables.** VG accepts only queries that start with `MATCH`, and the composite sends imported variables to a shard as a leading `WITH $@@x AS x`. So `CALL (x) { USE fibovg.lake … }` fails with `The query must start with at least one MATCH clause`. **Read the lake first, then pass its rows to the ontology** (queries 3–5). The consequence: a filter that comes from the ontology is applied in the composite, after the lake rows arrive, not pushed down to Trino. That is fine at this size, but it is the first thing to look at with real volumes. Two workarounds: put the class IRIs as literals in the lake query, or materialize the subclass closure as a lake table.

## Versions

Pinned: Neo4j `5.26.31-enterprise` (graph), `2026.09.0-enterprise` (VG), Trino and its JDBC driver `484`, n10s `5.26.0` (`N10S_VERSION`). Not pinned: `adobe/s3mock:latest`, `apache/iceberg-rest-fixture:latest`, and FIBO, which defaults to `master/latest/prod.ttl.zip` and can be set with `FIBO_URL`. Both Neo4j instances accept the evaluation license (`NEO4J_ACCEPT_LICENSE_AGREEMENT`, default `eval`).

## Not covered

- RBAC and per-user access to the lake. VG reads Trino as one user, and Trino allows everything.
- Apache Polaris. The catalog is Iceberg's REST fixture, the same API with no access control.
- FIBO's external imports (OMG Commons, LCC) are not in the production zip. Their IRIs appear only as bare `:Resource` nodes.
- Scale, latency, and pushdown of ontology-derived filters (see item 5 above).
- Certificate verification on the alias (`+ssc`).

## Files

| | |
| --- | --- |
| `run.sh` | up, fibo, check, shell, down. Makes the secrets in `.state/` (Neo4j password, alias keystore, VG's bolt certificate) and fetches the downloads. |
| `docker-compose.yml` | S3Mock, the Iceberg REST catalog, Trino, VG, and the Neo4j graph instance |
| `trino/iceberg.properties` | Trino's Iceberg catalog (REST catalog + S3) |
| `seed/seed.sql` | the lake's three tables, each row tagged with a FIBO class IRI |
| `vg/home/` | VG's data source (Trino), secret (user, empty password) and graph schema |
| `load_fibo.py` | loads the FIBO modules with n10s |
| `cypher/composite.cypher` | the composite database and its two aliases |
| `cypher/examples.cypher` | the example queries |
| `jdbc/`, `plugins/`, `fibo/` | downloads (untracked) |
