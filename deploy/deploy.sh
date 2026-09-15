#!/usr/bin/env bash
set -euo pipefail
umask 077

revision="${1:?Usage: deploy.sh COMMIT_SHA}"
[[ "$revision" =~ ^[0-9a-f]{40}$ ]] || exit 2
cd "$(dirname "$0")/.."
exec 9>/opt/balans/deploy.lock
flock -w 1200 9

test -r /etc/balans/runtime.env
test -r /etc/balans/migration.env
test -d /opt/balans/shared
docker compose version >/dev/null
export BALANS_IMAGE="balans:$revision"
compose=(docker compose -p balans -f deploy/production.compose.yaml)
# Build before stopping the existing bot. Failed builds leave it running.
docker build --label "org.opencontainers.image.revision=$revision" -t "$BALANS_IMAGE" .
"${compose[@]}" config --quiet

# Stop the sole receiver before changing the schema.
"${compose[@]}" stop bot
deployment_failed() {
  local status=$?
  trap - ERR
  echo "Deployment failed. Inspect the server; the database is not rolled back automatically." >&2
  "${compose[@]}" ps >&2 || true
  if [[ -n ${container_id:-} ]]; then
    docker inspect --format 'container={{.Name}} state={{.State.Status}} running={{.State.Running}} restarts={{.RestartCount}} exit={{.State.ExitCode}}' "$container_id" >&2 || true
    docker logs --tail 120 "$container_id" >&2 || true
  fi
  exit "$status"
}
trap deployment_failed ERR
docker run --rm --read-only --cap-drop ALL --security-opt no-new-privileges:true \
  --tmpfs /tmp:size=64m,mode=1777 \
  --add-host host.docker.internal:host-gateway \
  --network balans-backend \
  --env-file /etc/balans/migration.env \
  "$BALANS_IMAGE" python -m balans.migrate
"${compose[@]}" up -d --no-build --force-recreate bot

# Check that this process completed Telegram/DB startup and stays alive.
container_id="$("${compose[@]}" ps -q bot)"
test -n "$container_id"
started=false
for ((attempt=0; attempt<30; attempt++)); do
  state="$(docker inspect --format '{{.State.Running}} {{.RestartCount}}' "$container_id")"
  [[ "$state" == 'true 0' ]] || exit 1
  if docker logs "$container_id" 2>&1 | grep 'запущен (' >/dev/null; then
    started=true
    break
  fi
  sleep 4
done
[[ "$started" == true ]]
sleep 10
[[ "$(docker inspect --format '{{.State.Running}} {{.RestartCount}}' "$container_id")" == 'true 0' ]]
"${compose[@]}" exec -T bot python -m scripts.healthcheck
ln -sfn "/opt/balans/releases/$revision" /opt/balans/current
printf '%s\n' "$revision" > /opt/balans/last-successful-revision
echo "Deployed $revision"
