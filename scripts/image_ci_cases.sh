#!/usr/bin/env bash
# Entrypoint cases of the single image on native Linux (CI image lane):
#   - config directory owned by UID 0 (rootless Docker) and by an existing UID;
#   - data from the 0.3 Compose stack, whose replica set names mongo:27017,
#     is reconfigured to 127.0.0.1:27017, and a restart then skips the
#     preparation because the reconfigured data is recorded as ready.
# Usage: scripts/image_ci_cases.sh <image>   (needs sudo for chown)
set -euo pipefail

IMAGE="$1"
PORT=8181
WORK="$(mktemp -d)"
NAME=hybro-image-case

cleanup() {
    local status=$?
    if [ "$status" -ne 0 ]; then
        # The lane's failure step only dumps its own container.
        docker logs --tail=100 "$NAME" 2>&1 || true
    fi
    docker rm -f "$NAME" hybro-image-case-mongo >/dev/null 2>&1 || true
    docker volume rm -f hybro-image-case-data hybro-image-case-rs >/dev/null 2>&1 || true
    sudo rm -rf "$WORK"
}
trap cleanup EXIT

wait_healthy() {
    if ! timeout 180s bash -c "until curl -sf http://localhost:$PORT/health >/dev/null; do sleep 2; done"; then
        docker logs --tail=100 "$NAME"
        echo "Image did not become healthy" >&2
        exit 1
    fi
}

start() {  # start <config dir> <extra docker args...>
    local home="$1"
    shift
    docker run -d --name "$NAME" -p "127.0.0.1:$PORT:8080" \
        -e HYBRO_LISTEN=loopback -e "HYBRO_PUBLIC_PORT=$PORT" \
        -v "$home:/var/lib/hybro/runtime" --stop-timeout 60 "$@" "$IMAGE" >/dev/null
}

stop_clean() {
    timeout 70s docker stop "$NAME" >/dev/null
    test "$(docker inspect -f '{{.State.ExitCode}}' "$NAME")" = 0
    docker rm "$NAME" >/dev/null
}

for uid in 0 65534; do
    echo "== config directory owned by UID $uid"
    home="$WORK/home-$uid"
    mkdir -p "$home"
    sudo chown "$uid:$uid" "$home"
    sudo chmod 700 "$home"
    start "$home" -v hybro-image-case-data:/data
    wait_healthy
    test "$(sudo stat -c %u "$home/auth.json")" = "$uid"
    test "$(docker exec "$NAME" stat -c %u /data/db)" = "$uid"
    # The image has no procps; /proc needs no package.
    test "$(docker exec "$NAME" stat -c %u /proc/1)" = "$uid"
    # supervisord's root and socket notices would print on every start.
    if docker logs "$NAME" 2>&1 | grep -E " CRIT (Supervisor is running as root|Server 'unix_http_server' running without)"; then
        echo "supervisord logged a CRIT notice" >&2
        exit 1
    fi
    if [ "$uid" = 0 ]; then
        # Only root could write bytecode under the root-owned /app. Once every
        # agent and MCP listens, every program has imported its code; with
        # the image's bytecode current, none was written to the container.
        timeout 60s docker exec "$NAME" /app/backend/.venv/bin/python -c '
import socket, time, yaml
agents = yaml.safe_load(open("/app/bundled_agents.yaml"))["agents"].values()
ports = [agent["port"] for agent in agents if agent.get("enabled", True)]
for port in [8001, *ports]:
    while True:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=1).close()
            break
        except OSError:
            time.sleep(0.5)' || {
            echo "MCP or a bundled agent did not listen within 60 seconds" >&2
            exit 1
        }
        changes="$(docker diff "$NAME")"
        if printf '%s\n' "$changes" | grep '\.pyc$'; then
            echo "The running container compiled Python bytecode" >&2
            exit 1
        fi
    fi
    stop_clean
    docker volume rm hybro-image-case-data >/dev/null
done

echo "== migrated replica set (mongo:27017)"
docker run -d --name hybro-image-case-mongo --hostname mongo \
    -v hybro-image-case-rs:/data/db mongo:7.0 --replSet rs0 --bind_ip_all >/dev/null
timeout 60s bash -c 'until docker exec hybro-image-case-mongo mongosh --quiet --eval "db.adminCommand({ping: 1})" >/dev/null 2>&1; do sleep 1; done'
docker exec hybro-image-case-mongo mongosh --quiet --eval '
    rs.initiate({_id: "rs0", members: [{_id: 0, host: "mongo:27017"}]});
    while (!db.hello().isWritablePrimary) { sleep(500); }
    db.getSiblingDB("hybro").migration_probe.insertOne({kept: true});'
docker stop hybro-image-case-mongo >/dev/null
docker rm hybro-image-case-mongo >/dev/null

home="$WORK/home-migrated"
mkdir -p "$home"
chmod 700 "$home"
start "$home" -v hybro-image-case-rs:/data/db
wait_healthy
docker logs "$NAME" 2>&1 | grep "MongoDB replica set rs0: reconfigured" >/dev/null
test "$(docker exec "$NAME" /app/backend/.venv/bin/python -c '
from pymongo import MongoClient
print(MongoClient("mongodb://127.0.0.1:27017/?replicaSet=rs0").hybro.migration_probe.count_documents({}))')" = 1
timeout 70s docker restart "$NAME" >/dev/null
wait_healthy
docker logs "$NAME" 2>&1 | grep "MongoDB replica set rs0: ready (recorded)" >/dev/null
stop_clean
echo "Image entrypoint cases passed."
