// Run against `system` by ./run.sh up, with the parameter $vgPassword. Idempotent.
CREATE COMPOSITE DATABASE fibovg IF NOT EXISTS WAIT;

// shard 1: the FIBO ontology, a normal database on this instance
CREATE ALIAS fibovg.ontology IF NOT EXISTS FOR DATABASE fibo;

// shard 2: the Iceberg lake, the Virtual Graph instance's `neo4j` graph, reached over bolt.
// A remote alias must use TLS; +ssc accepts VG's self-signed certificate (use neo4j+s with a real one).
CREATE ALIAS fibovg.lake IF NOT EXISTS FOR DATABASE neo4j
  AT 'neo4j+ssc://vg:7687' USER neo4j PASSWORD $vgPassword;

SHOW ALIASES FOR DATABASE YIELD name, composite, database, location, url;
