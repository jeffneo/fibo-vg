// Example queries on the composite database `fibovg` (./run.sh check). Neo4j 5.26, Cypher 5.
// Shards: fibovg.ontology = FIBO (n10s, local), fibovg.lake = Iceberg via Virtual Graph (remote).
// The lake's rows carry `fibo_class` (a FIBO class IRI); the ontology's classes carry `uri`. That is the join.

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

// 3. Across both: each lake entity, its FIBO class and that class's definition
CALL () {
  USE fibovg.lake
  MATCH (e:LegalEntity)
  RETURN e.name AS entity, e.fibo_class AS cls
}
CALL (cls) {
  USE fibovg.ontology
  MATCH (c:Class {uri: cls})
  RETURN c.label AS fiboClass, c.definition AS definition
}
RETURN entity, fiboClass, left(definition, 90) AS definition
ORDER BY entity;

// 4. Ontology-driven: balances held by any kind of fund. "Fund" is decided by FIBO's class hierarchy (the holder's class
//    is a subclass of collective investment vehicle), not by a list written into the query.
//    The lake is read first: Virtual Graph accepts only queries that start with MATCH, so a lake subquery cannot import
//    variables (the composite would send them as a leading WITH). The ontology shard, a normal database, can.
CALL () {
  USE fibovg.lake
  MATCH (e:LegalEntity)-[:HOLDS]->(a:Account)
  RETURN e.name AS holder, e.fibo_class AS cls, count(a) AS accounts, sum(a.balance) AS held
}
CALL (cls) {
  USE fibovg.ontology
  MATCH (c:Class {uri: cls})-[:subClassOf*0..]->(:Class {uri: 'https://spec.edmcouncil.org/fibo/ontology/SEC/Securities/Pools/CollectiveInvestmentVehicle'})
  RETURN c.label AS fundType
  LIMIT 1
}
RETURN holder AS fund, fundType, accounts, held
ORDER BY held DESC;

// 5. Ontology-driven: accounts serviced by a depository institution (banks, credit unions, ...), by servicer and class
CALL () {
  USE fibovg.lake
  MATCH (a:Account)-[:SERVICED_BY]->(s:LegalEntity)
  RETURN s.name AS servicer, s.fibo_class AS cls, count(a) AS accounts
}
CALL (cls) {
  USE fibovg.ontology
  MATCH (c:Class {uri: cls})-[:subClassOf*0..]->(:Class {uri: 'https://spec.edmcouncil.org/fibo/ontology/FBC/FunctionalEntities/FinancialServicesEntities/DepositoryInstitution'})
  RETURN c.label AS servicerClass
  LIMIT 1
}
RETURN servicer, servicerClass, accounts
ORDER BY accounts DESC;
