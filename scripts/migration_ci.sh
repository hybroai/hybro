#!/usr/bin/env bash
# Migration lane (CI only): build 0.3-shaped volumes from fixtures and migrate
# them with `./scripts/hybro upgrade` from this checkout (dev mode: Compose
# project hybro-dev, target volume hybro-dev-data, port 3100).
#
# Checks: an old layout and an unfinished migration only notify; the upgrade
# stops the 0.3 container, keeps every collection count, agent ID, and room
# file, rewrites the bundled agent URL to loopback, and suggests the old port;
# cleanup-old then removes the 0.3 volumes.
#
# Usage: scripts/migration_ci.sh   (with hybro:dev built; creates and removes
# hybro-dev_* volumes, so never run it next to a real 0.3 checkout stack)
set -euo pipefail

cd "$(dirname "$0")/.."
PROJECT=hybro-dev
OLD_MONGO=hybro-dev-mongo-1
VOLUMES=(hybro-dev_mongo_data hybro-dev_room_files hybro-dev_redis_data)
HYBRO_HOME="$(mktemp -d)"
export HYBRO_HOME HYBRO_NO_UPDATE_CHECK=1
chmod 700 "$HYBRO_HOME"
PY=/app/backend/.venv/bin/python

cleanup() {
    local status=$?
    if [ "$status" -ne 0 ]; then
        docker logs --tail=100 hybro-dev 2>&1 || true
    fi
    docker rm -f hybro-dev "$OLD_MONGO" >/dev/null 2>&1 || true
    docker volume rm -f hybro-dev-data "${VOLUMES[@]}" >/dev/null 2>&1 || true
    rm -rf "$HYBRO_HOME"
}
trap cleanup EXIT

expect_failure() {  # expect_failure <text> <command...>
    local text="$1" output
    shift
    if output=$("$@" 2>&1); then
        echo "Expected failure from: $*" >&2
        echo "$output" >&2
        exit 1
    fi
    grep -F -- "$text" <<<"$output" >/dev/null || {
        echo "Missing \"$text\" in:" >&2
        echo "$output" >&2
        exit 1
    }
}

in_image() {  # in_image <python code>: run against the live container's MongoDB
    docker exec hybro-dev "$PY" -c "from pymongo import MongoClient
db = MongoClient('mongodb://127.0.0.1:27017/?replicaSet=rs0').hybro
$1"
}

echo "== 0.3 fixture: replica set named mongo:27017, rooms, agents, room files"
cat >"$HYBRO_HOME/config.json" <<'JSON'
{"version": 1, "instances": {"dev": {"ports": {"frontend": 3456}}}}
JSON
docker volume create hybro-dev_redis_data >/dev/null
docker run -d --name "$OLD_MONGO" --hostname mongo \
    --label "com.docker.compose.project=$PROJECT" \
    -v hybro-dev_mongo_data:/data/db mongo:7.0 --replSet rs0 --bind_ip_all >/dev/null
timeout 60s bash -c "until docker exec $OLD_MONGO mongosh --quiet --eval 'db.adminCommand({ping: 1})' >/dev/null 2>&1; do sleep 1; done"
docker exec "$OLD_MONGO" mongosh --quiet --eval '
    rs.initiate({_id: "rs0", members: [{_id: 0, host: "mongo:27017"}]});
    while (!db.hello().isWritablePrimary) { sleep(500); }
    const hybro = db.getSiblingDB("hybro");
    hybro.rooms.insertMany([{room_id: "r1"}, {room_id: "r2"}, {room_id: "r3"}]);
    hybro.agents.insertMany([
        {agent_id: "agent-echo-03", normalized_url: "http://echo-agent:7004",
         agent_card: {name: "Echo Agent", url: "http://echo-agent:7004"},
         agent_status: "active"},
        {agent_id: "agent-user-03", normalized_url: "http://example.test:9000",
         agent_card: {name: "User Agent", url: "http://example.test:9000"},
         agent_status: "active"}]);' >/dev/null
docker run --rm -v hybro-dev_room_files:/files mongo:7.0 \
    sh -c 'mkdir -p /files/r1 && echo kept > /files/r1/note.txt'

echo "== an old layout only notifies"
expect_failure "found data from Hybro 0.3" ./scripts/hybro -d
test "$(docker inspect -f '{{.State.Running}}' "$OLD_MONGO")" = true

echo "== an unfinished migration (copied data, no marker) only notifies"
docker volume create hybro-dev-data >/dev/null
docker run --rm -v hybro-dev-data:/data mongo:7.0 sh -c 'mkdir -p /data/db && touch /data/db/WiredTiger'
expect_failure "migration that did not finish" ./scripts/hybro -d
expect_failure "did not finish" ./scripts/hybro cleanup-old --yes

echo "== ./scripts/hybro upgrade"
output=$(./scripts/hybro upgrade 2>&1) || {
    echo "$output" >&2
    exit 1
}
echo "$output"
for text in "Migration complete" "cleanup-old" "--port 3456"; do
    grep -F -- "$text" <<<"$output" >/dev/null || {
        echo "Missing \"$text\" in the upgrade output" >&2
        exit 1
    }
done
if docker inspect "$OLD_MONGO" >/dev/null 2>&1; then
    echo "The 0.3 container was not removed" >&2
    exit 1
fi
timeout 180s bash -c 'until curl -sf http://localhost:3100/health >/dev/null; do sleep 2; done'

echo "== counts, IDs, files, and the rewritten bundled agent URL"
marker=$(docker exec hybro-dev cat /data/hybro-volume.json)
python3 - "$marker" <<'PY'
import json, sys
marker = json.loads(sys.argv[1])
assert marker["complete"] is True, marker
assert marker["migrated_from"] == "dev", marker
assert marker["collections"]["rooms"] == 3, marker
assert marker["collections"]["agents"] == 2, marker
assert marker["frontend_port"] == 3456, marker
PY
in_image 'assert db.rooms.count_documents({}) == 3'
in_image 'assert db.agents.find_one({"agent_id": "agent-user-03"})["normalized_url"] == "http://example.test:9000"'
test "$(docker exec hybro-dev cat /data/files/r1/note.txt)" = kept
# Boot registration reconciles the 0.3 record in place, keeping its agent_id.
timeout 120s bash -c "until docker exec hybro-dev $PY -c \"
from pymongo import MongoClient
db = MongoClient('mongodb://127.0.0.1:27017/?replicaSet=rs0').hybro
record = db.agents.find_one({'agent_id': 'agent-echo-03'})
assert record['normalized_url'] == 'http://localhost:7004', record['normalized_url']
assert record.get('bundled_name') == 'echo_agent'
assert db.agents.count_documents({'bundled_name': 'echo_agent'}) == 1
\" 2>/dev/null; do sleep 2; done"

echo "== the 0.3 volumes were only read"
docker run --rm -v hybro-dev_room_files:/files:ro mongo:7.0 cat /files/r1/note.txt | grep -x kept >/dev/null

echo "== a second upgrade has nothing to migrate; cleanup-old removes the 0.3 volumes"
./scripts/hybro upgrade | grep -F "up to date" >/dev/null
./scripts/hybro status >/dev/null
./scripts/hybro cleanup-old --yes
for volume in "${VOLUMES[@]}"; do
    if docker volume inspect "$volume" >/dev/null 2>&1; then
        echo "cleanup-old left $volume" >&2
        exit 1
    fi
done
docker volume inspect hybro-dev-data >/dev/null
timeout 70s ./scripts/hybro stop
test "$(docker inspect -f '{{.State.ExitCode}}' hybro-dev)" = 0
echo "Migration lane passed."
