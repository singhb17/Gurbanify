#!/usr/bin/env bash
# Start a local BaniDB so the import isn't rate limited.
#
# Run this in Git Bash:  ./start_local_banidb.sh
#
# Why not "npm run local"? Their scripts use Unix env-var syntax
# (DB_PORT=3002 node app.js) which cmd.exe can't parse, so npm run fails on
# Windows. These are the same two commands it would have run, done directly.

set -u

REPO="C:/Users/binwa/banidb-api"
CONTAINER=banidb-api
DB_PORT=3002
API_PORT=3001

# Docker Desktop installed per-user here rather than in Program Files, so any
# terminal opened before the install won't have it on PATH.
export PATH="$PATH:/c/Users/binwa/AppData/Local/Programs/DockerDesktop/resources/bin"

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker not found. Install Docker Desktop first:"
  echo "  https://www.docker.com/products/docker-desktop/"
  exit 1
fi

if ! docker info >/dev/null 2>&1; then
  echo "Docker is installed but the daemon isn't responding."
  echo "Start Docker Desktop and wait for the whale icon to stop animating."
  exit 1
fi

# 1. MySQL container -- the image ships with the Gurbani data already loaded.
if [ -n "$(docker ps -q -f name=^/${CONTAINER}$)" ]; then
  echo "container already running"
else
  echo "starting container..."
  docker start "$CONTAINER" 2>/dev/null || \
    docker run -d --name "$CONTAINER" \
      -e MYSQL_ROOT_PASSWORD=root \
      -e MYSQL_DATABASE=khajana_dev_khajana \
      -p ${DB_PORT}:3306 \
      khalisfoundation/banidb-dev:latest

  echo "waiting for MySQL to accept connections (first run pulls ~GBs, be patient)..."
  for i in $(seq 1 90); do
    if docker exec "$CONTAINER" mysqladmin ping -u root -proot 2>/dev/null | grep -q 'alive'; then
      echo "database up after ${i} checks"
      sleep 15          # it reports alive slightly before it's ready for queries
      break
    fi
    sleep 5
  done
fi

# 2. The API itself.
echo "starting API on http://localhost:${API_PORT}/v2/ ..."
cd "$REPO" || exit 1
DB_PORT=$DB_PORT DB_HOST=127.0.0.1 NODE_ENV=development node app.js
