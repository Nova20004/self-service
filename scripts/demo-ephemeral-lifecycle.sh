#!/usr/bin/env bash
# End-to-end lifecycle demo against a running stack started with the demo
# overlay (`gmake demo` does both). Every claim is checked; the script exits
# non-zero on the first failed check.
set -euo pipefail

cd "$(dirname "$0")/.."
[ -f .env ] || { echo "missing .env - run: gmake env" >&2; exit 2; }
set -a
# shellcheck disable=SC1091 # generated file
. ./.env
set +a

API="http://127.0.0.1:${API_PORT:-8000}"
EDGE="http://127.0.0.1:${EDGE_PORT:-8080}"
ENVCTL="python3 cli/envctl --url ${API}"
token_of() { printf '%s' "${OWNER_TOKENS}" | tr ',' '\n' | sed -n "s/^$1://p"; }
ALICE_TOKEN=$(token_of alice)
BOB_TOKEN=$(token_of bob)
PASSED=0

step() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
pass() { PASSED=$((PASSED + 1)); printf '\033[32mPASS\033[0m %s\n' "$*"; }
die()  { printf '\033[31mFAIL\033[0m %s\n' "$*" >&2; exit 1; }
as()   { local who=$1; shift; ENVCTL_TOKEN="$who" ${ENVCTL} "$@"; }
jget() { python3 -c 'import json,sys; print(json.load(sys.stdin)[sys.argv[1]])' "$1"; }
http_code() { curl -s -o /dev/null -w '%{http_code}' -H "Host: $1.localhost" "${EDGE}/"; }

wait_status() { # slug wanted timeout token
  local slug=$1 wanted=$2 timeout=$3 token=$4 status="" i
  for ((i = 0; i < timeout; i++)); do
    status=$(as "${token}" --json status "${slug}" | jget status)
    [ "${status}" = "${wanted}" ] && return 0
    sleep 1
  done
  die "${slug} did not reach ${wanted} within ${timeout}s (last: ${status})"
}

wait_route() { # slug wanted-code timeout
  local code="" i
  for ((i = 0; i < $3; i++)); do
    code=$(http_code "$1")
    [ "${code}" = "$2" ] && return 0
    sleep 1
  done
  die "route $1 returned ${code}, expected $2"
}

labelled_count() {
  local c n v
  c=$(docker ps -aq --filter label=managed-by=ephemeral-idp | wc -l | tr -d ' ')
  n=$(docker network ls -q --filter label=managed-by=ephemeral-idp | wc -l | tr -d ' ')
  v=$(docker volume ls -q --filter label=managed-by=ephemeral-idp | wc -l | tr -d ' ')
  echo "containers=${c} networks=${n} volumes=${v}"
}

step "0. control plane ready"
for i in $(seq 1 90); do
  curl -fsS "${API}/readyz" >/dev/null 2>&1 && break
  [ "${i}" = 90 ] && die "control plane not ready after 90s"
  sleep 1
done
curl -sS "${API}/readyz"; echo
pass "readyz reports database and docker proxy OK"

step "1. ChatOps: signed Slack slash command"
out=$(python3 scripts/sign-slack-request.py --env-file .env --send --url "${API}/v1/chatops/create-env" \
  --text "branch=feat-checkout ttl=10m" --user carol || true)
echo "${out}"
echo "${out}" | grep -q '^HTTP 200' || die "signed ChatOps request not accepted"
echo "${out}" | grep -q '"response_type":"ephemeral"' || die "response is not ephemeral"
echo "${out}" | grep -q 'PROVISIONING' || die "status PROVISIONING missing"
echo "${out}" | grep -q 'postgresql://app:' || die "DSN missing"
pass "signed /create-env answered with an ephemeral Slack message (URL, DSN, expiry, PROVISIONING)"
out=$(python3 scripts/sign-slack-request.py --env-file .env --send --tamper \
  --url "${API}/v1/chatops/create-env" --text "branch=evil ttl=10m" --user mallory || true)
echo "${out}"
echo "${out}" | grep -q '^HTTP 401' || die "tampered signature was not rejected"
pass "tampered Slack signature rejected with 401"

step "2. quotas: alice gets 3 environments, the 4th is refused"
as "${ALICE_TOKEN}" create feature/login --ttl 30m --wait
as "${ALICE_TOKEN}" create fix/payment-bug --ttl 30m
as "${ALICE_TOKEN}" create exp/search --ttl 30m
set +e
as "${ALICE_TOKEN}" create feature/fourth --ttl 30m
rc=$?
set -e
[ "${rc}" = 3 ] || die "4th env for alice should exit 3 (quota), got ${rc}"
pass "alice's 4th environment refused with HTTP 429 (envctl exit code 3)"
as "${BOB_TOKEN}" create bob/feature --ttl 30m
pass "bob is unaffected by alice's quota (202)"

step "3. authorization: bob cannot delete alice's environment"
set +e
err=$(as "${BOB_TOKEN}" delete feature-login 2>&1)
rc=$?
set -e
echo "${err}"
if [ "${rc}" != 4 ] || ! echo "${err}" | grep -q '403'; then
  die "expected 403 / exit 4, got rc=${rc}"
fi
pass "bob -> DELETE alice's env returned 403 (exit code 4)"

step "4. Traefik routes live traffic"
wait_route feature-login 200 30
page=$(curl -sS -H "Host: feature-login.localhost" "${EDGE}/")
echo "${page}" | sed 's/<[^>]*>/ /g' | tr -s ' ' | cut -c1-400
echo "${page}" | grep -q 'DB: connected - PostgreSQL 17' || die "page does not show DB connectivity"
pass "curl -H 'Host: feature-login.localhost' ${EDGE}/ -> 200, page shows DB connected"

step "5. extend moves expires_at"
before=$(as "${ALICE_TOKEN}" --json status feature-login | jget expires_at)
as "${ALICE_TOKEN}" extend feature-login --by 2h
after=$(as "${ALICE_TOKEN}" --json status feature-login | jget expires_at)
echo "before=${before} after=${after}"
[[ "${after}" > "${before}" ]] || die "expires_at did not move"
pass "extend moved expires_at from ${before} to ${after}"

step "6. free alice's slots, then create a 20 s environment"
wait_status fix-payment-bug RUNNING 90 "${ALICE_TOKEN}"
wait_status exp-search RUNNING 90 "${ALICE_TOKEN}"
as "${ALICE_TOKEN}" delete fix-payment-bug --wait
as "${ALICE_TOKEN}" delete exp-search --wait
as "${ALICE_TOKEN}" create demo/short-lived --ttl 20s --wait
wait_route demo-short-lived 200 15
pass "20 s environment demo-short-lived is RUNNING and routed (200)"

step "7. restart the control plane mid-life"
before_slugs=$(ENVCTL_TOKEN="${ADMIN_TOKEN}" ${ENVCTL} --json list \
  | python3 -c 'import json,sys; print(sorted(i["slug"] for i in json.load(sys.stdin)["items"]))')
docker restart eidp-control-plane >/dev/null
for i in $(seq 1 60); do
  curl -fsS "${API}/readyz" >/dev/null 2>&1 && break
  [ "${i}" = 60 ] && die "control plane did not come back"
  sleep 1
done
after_slugs=$(ENVCTL_TOKEN="${ADMIN_TOKEN}" ${ENVCTL} --json list \
  | python3 -c 'import json,sys; print(sorted(i["slug"] for i in json.load(sys.stdin)["items"]))')
echo "before restart: ${before_slugs}"
echo "after restart:  ${after_slugs}"
if [ "${before_slugs}" = "[]" ] || [ "${before_slugs}" != "${after_slugs}" ]; then
  die "state changed across restart"
fi
pass "state survived the restart (SQLite on a named volume)"

step "8. a manually created labelled orphan is reaped"
PG_IMAGE=$(docker inspect eidp-control-plane --format '{{range .Config.Env}}{{println .}}{{end}}' \
  | sed -n 's/^DB_IMAGE=//p')
docker network create --internal --label managed-by=ephemeral-idp \
  --label ephemeral.env-id=orphan-demo eidp-env-orphan-demo >/dev/null
docker volume create --label managed-by=ephemeral-idp --label ephemeral.env-id=orphan-demo \
  eidp-orphan-demo-pgdata >/dev/null
docker create --name eidp-orphan-demo-app --network eidp-env-orphan-demo \
  --label managed-by=ephemeral-idp --label ephemeral.env-id=orphan-demo "${PG_IMAGE}" >/dev/null
echo "created orphan container, network and volume ($(labelled_count))"
for i in $(seq 1 45); do
  left=$(docker ps -aq --filter name=eidp-orphan-demo-app; \
         docker network ls -q --filter name=eidp-env-orphan-demo; \
         docker volume ls -q --filter name=eidp-orphan-demo-pgdata)
  [ -z "${left}" ] && break
  [ "${i}" = 45 ] && die "orphan resources still present after 45s"
  sleep 1
done
pass "orphan container + network + volume removed by the reconciler after the grace period"

step "9. the 20 s environment expires on its own"
wait_status demo-short-lived DROPPED 60 "${ALICE_TOKEN}"
as "${ALICE_TOKEN}" status demo-short-lived
wait_route demo-short-lived 404 15
pass "demo-short-lived DROPPED by the reaper after the restart; its route now returns 404"

step "10. audit trail and FinOps"
ENVCTL_TOKEN="${ADMIN_TOKEN}" ${ENVCTL} audit --limit 12
curl -sS -H "Authorization: Bearer ${ADMIN_TOKEN}" "${API}/v1/finops/summary"; echo
curl -sS "${API}/metrics" | grep -E '^ephemeral_(envs_active|envs_created_total|envs_reaped_total|quota_rejections_total|orphans_reaped_total)' || true
pass "audit log and FinOps summary available to admin"

step "11. tear everything down through the API"
wait_status bob-feature RUNNING 90 "${BOB_TOKEN}"
wait_status feat-checkout RUNNING 90 "${ADMIN_TOKEN}"
as "${ALICE_TOKEN}" delete feature-login --wait
as "${BOB_TOKEN}" delete bob-feature --wait
ENVCTL_TOKEN="${ADMIN_TOKEN}" ${ENVCTL} delete feat-checkout --wait
ENVCTL_TOKEN="${ADMIN_TOKEN}" ${ENVCTL} list --all
counts=$(labelled_count)
echo "labelled resources: ${counts}"
[ "${counts}" = "containers=0 networks=0 volumes=0" ] || die "labelled resources remain"
pass "no labelled container, network or volume remains"

printf '\n\033[32mDEMO OK\033[0m: %s checks passed\n' "${PASSED}"
