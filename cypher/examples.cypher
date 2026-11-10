// Example queries on the composite database `fibovg` (./run.sh check). Neo4j 5.26, Cypher 5.
// Shards: fibovg.lake      Iceberg via Virtual Graph (remote): the business's own codes, no FIBO
//         fibovg.semantic  the semantic layer: what the lake's tables, columns and codes mean in FIBO
//         fibovg.ontology  FIBO, loaded with n10s
// The lake is always read first: VG accepts only queries that start with MATCH, so a lake subquery cannot import
// variables (the composite would send them as a leading WITH). The other two shards, normal databases, can.

// 1. The lake on its own: who holds which account, serviced by whom
USE fibovg.lake
MATCH (holder:LegalEntity)-[:HOLDS]->(a:Account)-[:SERVICED_BY]->(servicer:LegalEntity)
RETURN a.account_id AS account, holder.name AS holder, a.product AS product, servicer.name AS servicer
ORDER BY account;

// 2. The ontology on its own: FIBO's superclasses of "hedge fund"
USE fibovg.ontology
MATCH path = (:Class {label: 'hedge fund'})-[:subClassOf*1..]->(sup:Class)
WHERE sup.uri STARTS WITH 'https://spec.edmcouncil.org/fibo/'
RETURN sup.label AS superclass, length(path) AS distance
ORDER BY distance, superclass;

// 3. The semantic layer on its own: where each FIBO property lives in the lake (graph property, and physical column)
USE fibovg.semantic
MATCH (k:Concept)<-[m:MAPS_TO|VALUE_CLASS]-(p:PropertyMap)-[:FROM_COLUMN]->(c:Column)
RETURN coalesce(k.label, k.iri) AS fibo, type(m) = 'VALUE_CLASS' AS valueOfClass, p.owner + '.' + p.name AS graph, c.fqn AS column
ORDER BY fibo;

// 4. All three: each lake entity, its FIBO class (by its code, through the semantic layer) and that class's definition
CALL () {
  USE fibovg.lake
  MATCH (e:LegalEntity)
  RETURN e.name AS entity, e.entity_type AS code
}
CALL (code) {
  USE fibovg.semantic
  MATCH (:NodeMap {label: 'LegalEntity'})-[:HAS_RULE]->(:ClassRule {value: code})-[:ASSIGNS]->(k:Concept)
  RETURN k.iri AS cls
}
CALL (cls) {
  USE fibovg.ontology
  MATCH (c:Class {uri: cls})
  RETURN c.label AS fiboClass, c.definition AS definition
}
RETURN entity, code, fiboClass, left(definition, 80) AS definition
ORDER BY entity;

// 5. Classified by the ontology: balances held by any kind of fund. "Fund" is a subclass of collective investment
//    vehicle in FIBO; which codes mean which class is the semantic layer's. Neither is written into the query.
CALL () {
  USE fibovg.lake
  MATCH (e:LegalEntity)-[:HOLDS]->(a:Account)
  RETURN e.name AS holder, e.entity_type AS code, count(a) AS accounts, sum(a.balance) AS held
}
CALL (code) {
  USE fibovg.semantic
  MATCH (:NodeMap {label: 'LegalEntity'})-[:HAS_RULE]->(:ClassRule {value: code})-[:ASSIGNS]->(k:Concept)
  RETURN k.iri AS cls
}
CALL (cls) {
  USE fibovg.ontology
  MATCH (c:Class {uri: cls})-[:subClassOf*0..]->(:Class {uri: 'https://spec.edmcouncil.org/fibo/ontology/SEC/Securities/Pools/CollectiveInvestmentVehicle'})
  RETURN c.label AS fundType
  LIMIT 1
}
RETURN holder AS fund, fundType, accounts, held
ORDER BY held DESC;

// 6. Classified by the ontology: accounts serviced by a depository institution (banks, credit unions, ...)
CALL () {
  USE fibovg.lake
  MATCH (a:Account)-[:SERVICED_BY]->(s:LegalEntity)
  RETURN s.name AS servicer, s.entity_type AS code, count(a) AS accounts
}
CALL (code) {
  USE fibovg.semantic
  MATCH (:NodeMap {label: 'LegalEntity'})-[:HAS_RULE]->(:ClassRule {value: code})-[:ASSIGNS]->(k:Concept)
  RETURN k.iri AS cls
}
CALL (cls) {
  USE fibovg.ontology
  MATCH (c:Class {uri: cls})-[:subClassOf*0..]->(:Class {uri: 'https://spec.edmcouncil.org/fibo/ontology/FBC/FunctionalEntities/FinancialServicesEntities/DepositoryInstitution'})
  RETURN c.label AS servicerClass
  LIMIT 1
}
RETURN servicer, servicerClass, accounts
ORDER BY accounts DESC;

// 7. Ontology + semantic layer, no lake: which columns and codes in the lake identify a collective investment vehicle
CALL () {
  USE fibovg.ontology
  MATCH (:Class {uri: 'https://spec.edmcouncil.org/fibo/ontology/SEC/Securities/Pools/CollectiveInvestmentVehicle'})<-[:subClassOf*0..]-(s:Class)
  RETURN collect(DISTINCT s.uri) AS classes
}
CALL (classes) {
  USE fibovg.semantic
  MATCH (n:NodeMap)-[:HAS_RULE]->(r:ClassRule)-[:ASSIGNS]->(k:Concept), (r)-[:ON_COLUMN]->(c:Column)
  WHERE k.iri IN classes
  RETURN n.label AS label, c.fqn AS column, r.value AS code, k.label AS fiboClass
}
RETURN label, column, code, fiboClass
ORDER BY code;

// 8. The semantic layer's own caveats: approximate mappings, and terms FIBO imports but this load does not have
USE fibovg.semantic
CALL () {
  MATCH (x) WHERE x.note IS NOT NULL
  RETURN labels(x)[0] AS kind, coalesce(x.owner + '.' + x.name, x.value, x.type) AS item, x.note AS note
  UNION
  MATCH (k:Concept {external: true})
  RETURN 'Concept' AS kind, k.iri AS item, 'imported by FIBO from OMG Commons, which is not loaded: no label or definition' AS note
}
RETURN kind, item, note
ORDER BY kind, item;
