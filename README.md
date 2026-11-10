# FIBO + Iceberg in one composite database, with a semantic layer between them

Scaffolding for this architecture:
- an **Iceberg data lake** on one side, served as a graph by a self-managed **Neo4j Virtual Graph (VG)**
- the **FIBO ontology** on the other, loaded into Neo4j with **neosemantics (n10s)**
- in the middle, a **semantic layer** that maps ontology elements to the lake's tables, columns and codes

All three are **shards of one composite database**. Local Docker only; nothing is published beyond `127.0.0.1`.

No RBAC and no credential pass-through: Trino has no authentication, and VG reads it as a single user. For that, see `neovg-trino-iceberg`, which this is adapted from.

```bash
./run.sh up                    # object store, Iceberg catalog, Trino (seeded), VG, Neo4j with FIBO, the semantic layer, the composite
./run.sh check                 # the example queries in cypher/examples.cypher, on the composite database
./run.sh ask "hedge fund"      # ask the lake in FIBO's terms: a class, then any FIBO properties wanted
./run.sh semantic              # rebuild the semantic layer after editing semantic/mapping.yaml (up does it every time)
./run.sh fibo                  # reload FIBO (up does it when the database is empty)
./run.sh shell                 # cypher-shell on the composite database
./run.sh down                  # remove the containers and their data (downloads and .state/ stay)
```

Needs Docker, `uv`, `keytool` (any JDK), `openssl`, `curl`, `unzip`, `zip`. On the first run, `run.sh` downloads Trino's JDBC driver (checked against Maven Central's SHA-1), n10s, and FIBO's production release.

## What it is

```
                                   graph: Neo4j 5.26 Enterprise  (bolt://localhost:7690)
                                   ┌──────────────────────────────────────────────────────────────┐
  Cypher ──▶ composite  fibovg ──┬─▶ fibovg.ontology ─▶ database fibo      (FIBO via n10s)        │
             ask.py ─────────────┤                                                                │
                                 ├─▶ fibovg.semantic ─▶ database semantic  (semantic/mapping.yaml) │
                                 │                                                                │
                                 └─▶ fibovg.lake ──── remote alias, neo4j+ssc://vg:7687 ──────────┼──┐
                                   └──────────────────────────────────────────────────────────────┘  │
                                                                                                     ▼
  vg: Neo4j 2026.09 Enterprise, Virtual Graph (bolt://localhost:7691) ── jdbc:trino ──▶ Trino 484 (http://localhost:8081)
                                                                                         │ REST
                                                                                         ▼
                                                                    Iceberg REST catalog ──▶ S3Mock (Parquet files)
```

| Shard | What | How |
| --- | --- | --- |
| `fibovg.lake` | 3 Iceberg tables in `iceberg.lake`: `legal_entities` (12), `accounts` (10), `ownership` (5). The rows carry the business's own codes (`entity_type` such as `HF` or `BANK`, `product` such as `custody` or `prime`) and nothing from FIBO. | VG's `generic` JDBC data source on Trino ([vg/home/](vg/home/)). The graph is `(:LegalEntity)-[:HOLDS]->(:Account)-[:SERVICED_BY]->(:LegalEntity)` and `(:LegalEntity)-[:OWNS {pct}]->(:LegalEntity)`. |
| `fibovg.semantic` | What the lake's tables, columns and codes mean in FIBO | `load_semantic.py`, from [semantic/mapping.yaml](semantic/mapping.yaml). See below. |
| `fibovg.ontology` | FIBO production (156 modules, about 134k triples, 2,544 classes) | `load_fibo.py` sends each module's Turtle to `n10s.rdf.import.inline`. The graph config is `handleVocabUris: IGNORE`, so classes read as `(:Class {uri, label, definition})-[:subClassOf]->(:Class)`. |

## The semantic layer

[semantic/mapping.yaml](semantic/mapping.yaml) is the only hand-written part. It works in the spirit of R2RML. It says:

- **Table → class:** every `legal_entities` row is a legal entity, and every `accounts` row is an account.
- **Code → class:** the rows whose code has a given value are instances of a more specific class. For example, `entity_type = 'HF'` is a hedge fund, `entity_type = 'BANK'` is a commercial bank, and `product = 'prime'` is a brokerage account. This is how rows get a FIBO class without the lake knowing FIBO.
- **Column → property:** `name` is *has legal name*, `jurisdiction` is *is incorporated in*, `balance` is *has balance*, and `pct` on `OWNS` is *has ownership percentage*. A column can also say what class of value it holds: `lei` holds a legal entity identifier.
- **Relationship → object property and roles:** `HOLDS` is *has primary account holder*, in the inverse direction, and its start plays *account holder*. `OWNS` is *owns*. `SERVICED_BY` has no FIBO property, so its end plays *account provider*.
- **Notes** where a mapping is approximate. For example, FIBO has no custody account class, so `custody` maps to investment account, with a note.

`load_semantic.py` builds the `semantic` database from three sources, so the mapping is tied to the physical layer and not only to names:

```
physical   (:Table)-[:HAS_COLUMN]->(:Column)                          the lake's own catalog (Trino's information_schema)
graph      (:NodeMap)-[:FROM_TABLE]->(:Table), -[:KEY]->(:Column)     VG's mapping (vg/home/schema.json)
           (:NodeMap)-[:HAS_PROPERTY]->(:PropertyMap)-[:FROM_COLUMN]->(:Column)
           (:RelMap)-[:START|END]->(:NodeMap), -[:START_KEY|END_KEY]->(:Column)
semantic   (:NodeMap|PropertyMap|RelMap)-[:MAPS_TO]->(:Concept {iri, label})          from mapping.yaml
           (:NodeMap)-[:HAS_RULE]->(:ClassRule {value})-[:ASSIGNS]->(:Concept), (:ClassRule)-[:ON_COLUMN]->(:Column)
           (:PropertyMap)-[:VALUE_CLASS]->(:Concept), (:RelMap)-[:START_ROLE|END_ROLE]->(:Concept)
```

The loader checks every name in the mapping against VG's schema and the lake's columns, and every IRI against the FIBO database. A typo fails the load, and the error names it:

```
error: IRI not in FIBO: https://spec.edmcouncil.org/fibo/ontology/SEC/Funds/Funds/HedgeFnud
error: nodes.Account.properties.balanse: VG's Account has no property balanse
```

Two IRIs come from OMG Commons, which FIBO imports but the production zip leaves out: *legal entity* and *jurisdiction*. They exist in the graph without a label, and the loader accepts them with a warning. On success it prints coverage: 13 columns, 7 with a FIBO meaning; the other 6 are code columns or join keys, and none is left without a role.

`Concept` nodes keep a copy of the FIBO label for readability. The ontology shard stays the source of truth, and the queries join on the IRI.

### Asking in FIBO's terms: `ask.py`

```
$ ./run.sh ask "depository institution" "has legal name" "is incorporated in"
class      depository institution  <…/FinancialServicesEntities/DepositoryInstitution>  (32 with its subclasses)

label      LegalEntity  (table iceberg.lake.legal_entities)
rows       by code: BANK = commercial bank, CU = credit union
columns    lei = legal entity identifier, name = has legal name, jurisdiction = is incorporated in
query      USE fibovg.lake MATCH (n:`LegalEntity`) WHERE n.`entity_type` = $code0 OR n.`entity_type` = $code1 RETURN …

  legal entity identifier  has legal name                    is incorporated in  FIBO class (from the code)
  EXAMPLE0000000000001     Harborline Bank N.A.              US-NY               commercial bank
  EXAMPLE0000000000012     Lakeshore Community Credit Union  US-MI               credit union
```

Each step goes through the composite, one shard at a time:
1. **Ontology:** the class and its subclasses, and each property and its subproperties.
2. **Semantic layer:** the labels mapped to those classes, either the whole table or the rows with given codes, and the properties mapped to the requested ones.
3. **Lake:** the generated query, run on VG.

Here the filter on the codes is pushed down to Trino. Other examples: `./run.sh ask "collective investment vehicle"`, `./run.sh ask account "has balance"`, and `./run.sh ask fibo-fbc-acct:BrokerageAccount`. Classes and properties can be named by FIBO label, IRI, or `prefix:name` from the mapping.

The translation needs code outside Neo4j for two reasons. A lake subquery can't take variables from another shard (finding 5 below), and VG doesn't accept dynamic labels or property names. `ask.py` supports one class and its datatype properties; it doesn't follow relationships yet.

## Example queries ([cypher/examples.cypher](cypher/examples.cypher))

1. **Lake only:** who holds which account, and who services it.
2. **Ontology only:** FIBO's superclasses of "hedge fund" (open-end investment → managed investment → collective investment vehicle → …).
3. **Semantic layer only:** where each FIBO property lives, as both a graph property and a physical column.
4. **All three:** each lake entity, its code, the FIBO class that code means, and that class's definition.
5. **Classified by the ontology:** balances held by *any kind of fund*. "Fund" means a subclass of collective investment vehicle in FIBO, and which codes mean which class comes from the semantic layer. Neither is written into the query. The result is the ETF, the mutual fund and the hedge fund. The pension fund drops out, because FIBO does not make a pension fund a collective investment vehicle.
6. **Classified by the ontology:** accounts serviced by a depository institution (the commercial bank and the credit union, but not the broker-dealer).
7. **Ontology + semantic layer, no lake:** which column and which codes identify a collective investment vehicle.
8. **The semantic layer's caveats:** approximate mappings, and the Commons terms with no label.

The pattern for queries across shards. The lake is always read first, because a lake subquery can't import variables (finding 5):

```cypher
CALL ()     { USE fibovg.lake     MATCH (e:LegalEntity) RETURN e.name AS entity, e.entity_type AS code }
CALL (code) { USE fibovg.semantic MATCH (:NodeMap {label: 'LegalEntity'})-[:HAS_RULE]->(:ClassRule {value: code})-[:ASSIGNS]->(k) RETURN k.iri AS cls }
CALL (cls)  { USE fibovg.ontology MATCH (c:Class {uri: cls}) RETURN c.label AS fiboClass }
RETURN entity, fiboClass
```

## What I found while building it (2026-11-09 and 10)

1. **n10s does not run on Neo4j 2026.09.** No n10s release lists 2026.x in its `versions.json`, so `NEO4J_PLUGINS='["n10s"]'` cannot install it. The latest jar (2025.06.1) loads only in part: 2026.09's procedure classloader fails on n10s's package-private nested classes (`IllegalAccessError`), and `n10s.graphconfig.*`, `n10s.nsprefixes.*`, `n10s.add.*` and the mapping procedures are missing. Without `graphconfig.init`, nothing can be imported. Writing the config node by hand does not work either: n10s reads its settings as Java `Integer`, and Cypher stores `Long`. **The graph instance is therefore Neo4j 5.26 LTS with n10s 5.26.0**, and VG stays on 2026.09. The composite reaches VG over bolt, and mixing the two versions worked.
2. **n10s 5.26.0 stops Neo4j 5.26.31 from starting** (`NoSuchMethodError: JsonProperty.isRequired`). It is a fat jar with an old, unshaded `jackson-annotations`, and the image puts `/plugins/*` ahead of Neo4j's `lib/*` on the classpath. `run.sh` mounts a copy with `com/fasterxml/jackson/*` removed (`plugins/neosemantics-5.26.0-nojackson.jar`). After that, all 55 n10s procedures load. The only remaining load warnings are for optional HTTP-cache classes (memcached, ehcache).
3. **A remote alias has to use TLS.** Neo4j 5.26 refuses `neo4j://` for `CREATE ALIAS … AT` and asks for `neo4j+s` or `neo4j+ssc`. VG's bolt has a self-signed certificate (`.state/vg-bolt/`) at `tls_level: OPTIONAL`, and the alias uses `neo4j+ssc` (encrypted, certificate not verified). With a real certificate, use `neo4j+s`. A remote alias also needs a keystore for the stored password (`.state/aliases.p12`, settings in `docker-compose.yml`).
4. **VG requires a `secret.json`** even when Trino has no authentication. A `basic` secret with an empty password works. The user then has to come from the secret and not the JDBC URL, or the driver refuses: `user is passed both by URL and properties`.
5. **A lake subquery cannot import variables.** VG accepts only queries that start with `MATCH`, and the composite sends imported variables to a shard as a leading `WITH $@@x AS x`. So `CALL (x) { USE fibovg.lake … }` fails with `The query must start with at least one MATCH clause`. In a single Cypher query, **read the lake first, then pass its rows to the semantic layer and the ontology** (queries 4–7). The consequence: in those queries, a filter that comes from the ontology is applied in the composite, after the lake rows arrive, not pushed down to Trino. `ask.py` is the way around it. It resolves the ontology and the semantic layer first and writes the codes into the lake query, so the filter reaches Trino.
6. **VG's default SQL dialect has no `IN`.** `WHERE n.entity_type IN $codes` fails with `default dialect does not support …Expression$In`. `ask.py` writes one equality per code, joined with `OR`.

## Versions

Pinned: Neo4j `5.26.31-enterprise` (graph), `2026.09.0-enterprise` (VG), Trino and its JDBC driver `484`, n10s `5.26.0` (`N10S_VERSION`). Not pinned: `adobe/s3mock:latest`, `apache/iceberg-rest-fixture:latest`, and FIBO, which defaults to `master/latest/prod.ttl.zip` and can be set with `FIBO_URL`. Both Neo4j instances accept the evaluation license (`NEO4J_ACCEPT_LICENSE_AGREEMENT`, default `eval`).

## Not covered

- RBAC and per-user access to the lake. VG reads Trino as one user, and Trino allows everything.
- Apache Polaris. The catalog is Iceberg's REST fixture, the same API with no access control.
- FIBO's external imports (OMG Commons, LCC) are not in the production zip. Their IRIs appear only as bare `:Resource` nodes, so *legal entity* and *jurisdiction* have no label or definition here.
- In the semantic layer: mappings that need a transform (a code to a FIBO individual, units, a date format), classes decided by more than one column, and `ask.py` following relationships.
- Scale, latency, and pushdown of ontology-derived filters in plain Cypher (see finding 5).
- Certificate verification on the alias (`+ssc`).

## Files

| | |
| --- | --- |
| `run.sh` | up, check, ask, semantic, fibo, shell, down. Makes the secrets in `.state/` (Neo4j password, alias keystore, VG's bolt certificate) and fetches the downloads. |
| `docker-compose.yml` | S3Mock, the Iceberg REST catalog, Trino, VG, and the Neo4j graph instance |
| `trino/iceberg.properties` | Trino's Iceberg catalog (REST catalog + S3) |
| `seed/seed.sql` | the lake's three tables, with the business's codes and no FIBO |
| `vg/home/` | VG's data source (Trino), secret (user, empty password) and graph schema |
| `semantic/mapping.yaml` | the semantic layer: tables, codes, columns and relationships mapped to FIBO |
| `load_semantic.py` | builds and validates the `semantic` database from the mapping, VG's schema and the lake's catalog |
| `ask.py` | asks the lake in FIBO's terms through the semantic layer |
| `load_fibo.py` | loads the FIBO modules with n10s |
| `cypher/composite.cypher` | the composite database and its three aliases |
| `cypher/examples.cypher` | the example queries |
| `jdbc/`, `plugins/`, `fibo/` | downloads (untracked) |
