#!/usr/bin/env bash
# FIBO + Iceberg in one composite database. Local Docker only.
#   ./run.sh up         everything: object store, Iceberg catalog, Trino (seeded), Virtual Graph, Neo4j with FIBO, the semantic layer, the composite database
#   ./run.sh fibo       (re)load FIBO into the `fibo` database with neosemantics (done by `up` when the database is empty)
#   ./run.sh semantic   (re)build the semantic layer from semantic/mapping.yaml (done by every `up`)
#   ./run.sh ask CLASS [PROPERTY...]   ask the lake in FIBO's terms, e.g. ./run.sh ask "hedge fund" "has legal name"
#   ./run.sh check      run the example queries in cypher/examples.cypher against the composite database
#   ./run.sh shell      a cypher-shell on the composite database
#   ./run.sh down       remove everything (containers and their data; downloads and .state/ stay)
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
STATE=.state; mkdir -p "$STATE"
say() { echo "fibo-vg: $*"; }

# Secrets, machine-made and untracked: one Neo4j password for both instances, and the keystore that encrypts the remote alias's password.
[ -f "$STATE/neo4j-password" ] || openssl rand -hex 12 > "$STATE/neo4j-password"
[ -f "$STATE/keystore-password" ] || openssl rand -hex 12 > "$STATE/keystore-password"
export NEO4J_PASSWORD KEYSTORE_PASSWORD
NEO4J_PASSWORD=$(cat "$STATE/neo4j-password")
KEYSTORE_PASSWORD=$(cat "$STATE/keystore-password")
if [ ! -f "$STATE/aliases.p12" ]; then
  keytool -genseckey -keyalg aes -keysize 256 -storetype pkcs12 -alias aliases \
    -keystore "$STATE/aliases.p12" -storepass "$KEYSTORE_PASSWORD" >/dev/null 2>&1
  chmod 644 "$STATE/aliases.p12" # read by the container's neo4j user
fi
# VG's bolt certificate, self-signed: a remote alias must use TLS (neo4j+s / neo4j+ssc), so VG's bolt offers it.
if [ ! -f "$STATE/vg-bolt/public.crt" ]; then
  mkdir -p "$STATE/vg-bolt"
  openssl req -x509 -newkey rsa:2048 -nodes -days 365 -subj "/CN=vg" -addext "subjectAltName=DNS:vg,DNS:localhost" \
    -keyout "$STATE/vg-bolt/private.key" -out "$STATE/vg-bolt/public.crt" >/dev/null 2>&1
  chmod 644 "$STATE/vg-bolt/private.key" # read by the container's neo4j user; a local, throwaway key
fi

# Downloads, untracked: Trino's JDBC driver (checked against Maven Central's SHA-1), neosemantics, FIBO.
export N10S_VERSION=${N10S_VERSION:-5.26.0}
FIBO_URL=${FIBO_URL:-https://spec.edmcouncil.org/fibo/ontology/master/latest/prod.ttl.zip}
fetch() { # url dest [sha1-url]
  [ -f "$2" ] && return 0
  mkdir -p "$(dirname "$2")"; say "downloading $1"
  curl -fsSL "$1" -o "$2.part"
  if [ -n "${3:-}" ] && [ "$(shasum "$2.part" | cut -d' ' -f1)" != "$(curl -fsSL "$3")" ]; then
    rm -f "$2.part"; echo "fibo-vg: checksum mismatch for $1" >&2; exit 1
  fi
  mv "$2.part" "$2"
}
JDBC=https://repo1.maven.org/maven2/io/trino/trino-jdbc/484/trino-jdbc-484.jar
fetch "$JDBC" jdbc/trino-jdbc-484.jar "$JDBC.sha1"
N10S_JAR=plugins/neosemantics-$N10S_VERSION.jar
fetch "https://github.com/neo4j-labs/neosemantics/releases/download/$N10S_VERSION/neosemantics-$N10S_VERSION.jar" "$N10S_JAR"
# n10s is a fat jar with Jackson unshaded, and the image puts /plugins/* ahead of Neo4j's own lib/* on the classpath: its old
# jackson-annotations stops Neo4j 5.26.31 from starting (NoSuchMethodError: JsonProperty.isRequired). Mount a copy without it.
if [ ! -f "${N10S_JAR%.jar}-nojackson.jar" ]; then
  cp "$N10S_JAR" "${N10S_JAR%.jar}-nojackson.jar"
  zip -q -d "${N10S_JAR%.jar}-nojackson.jar" 'com/fasterxml/jackson/*' 'META-INF/maven/com.fasterxml.jackson.core/*'
fi
if [ ! -d fibo/ttl ]; then
  fetch "$FIBO_URL" fibo/prod.ttl.zip
  mkdir -p fibo/ttl && unzip -q -o fibo/prod.ttl.zip -d fibo/ttl
fi

trino() { docker exec -i fibo-vg-trino trino "$@"; }
cypher() { local c=$1 db=$2; shift 2; docker exec -i "$c" cypher-shell -u neo4j -p "$NEO4J_PASSWORD" -d "$db" "$@"; }
wait_for() { # what, command...
  local what=$1 i; shift
  for i in $(seq 1 80); do "$@" >/dev/null 2>&1 && return 0; sleep 3; done
  echo "fibo-vg: $what did not come up" >&2; return 1
}

data() { # the object store, the catalog and Trino, with the lake's tables
  docker compose up -d s3 rest trino
  say "waiting for Trino"; wait_for Trino trino --execute "SELECT 1"
  local tables; tables=$(trino --execute "SHOW TABLES FROM iceberg.lake" 2>/dev/null || true) # captured first: under pipefail, grep -q can kill its writer
  if ! grep -q legal_entities <<<"$tables"; then say "seeding Iceberg"; trino < seed/seed.sql >/dev/null; fi
}

load_fibo() {
  say "loading FIBO into the database fibo (neosemantics)"
  cypher fibo-vg-graph system "CREATE DATABASE fibo IF NOT EXISTS WAIT" >/dev/null
  uv run -q --with neo4j python load_fibo.py fibo/ttl
}
load_semantic() { uv run -q --with neo4j --with pyyaml python load_semantic.py; }

case "${1:-}" in
  up)
    data
    docker compose up -d vg graph
    say "waiting for Virtual Graph"; wait_for "Virtual Graph" cypher fibo-vg-vg neo4j "RETURN 1"
    say "waiting for Neo4j";        wait_for Neo4j cypher fibo-vg-graph system "SHOW DATABASES"
    cypher fibo-vg-graph system "CREATE DATABASE fibo IF NOT EXISTS WAIT" >/dev/null
    n=$(cypher fibo-vg-graph fibo --format plain "MATCH (n) RETURN count(n) > 0" | tail -1)
    [ "$n" = TRUE ] || load_fibo
    load_semantic
    say "creating the composite database"
    cypher fibo-vg-graph system --format plain -P "{vgPassword: '$NEO4J_PASSWORD'}" < cypher/composite.cypher | sed -n '/^name, /,$p'
    say "up: Neo4j bolt://localhost:7690 (composite: fibovg), Virtual Graph bolt://localhost:7691, Trino http://localhost:8081 (password in $STATE/neo4j-password)"
    ;;
  fibo) load_fibo ;;
  semantic) load_semantic ;;
  ask) shift; exec uv run -q --with neo4j python ask.py "$@" ;;
  check) cypher fibo-vg-graph fibovg --format verbose < cypher/examples.cypher ;;
  shell) docker exec -it fibo-vg-graph cypher-shell -u neo4j -p "$NEO4J_PASSWORD" -d fibovg ;;
  down) docker compose down -v ;;
  *) sed -n '2,9p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
