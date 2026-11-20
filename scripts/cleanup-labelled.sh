#!/usr/bin/env bash
# Remove every container, network and volume labelled managed-by=ephemeral-idp.
# Never touches anything else. Safe to run repeatedly.
set -euo pipefail

LABEL="managed-by=ephemeral-idp"
TRAEFIK="${TRAEFIK_CONTAINER:-eidp-traefik}"

containers=$(docker ps -aq --filter "label=${LABEL}")
if [ -n "${containers}" ]; then
  echo "removing labelled containers: $(echo "${containers}" | wc -l | tr -d ' ')"
  # shellcheck disable=SC2086 # word splitting of IDs is intended
  docker rm -f ${containers} >/dev/null
fi

networks=$(docker network ls -q --filter "label=${LABEL}")
for net in ${networks}; do
  docker network disconnect -f "${net}" "${TRAEFIK}" >/dev/null 2>&1 || true
  docker network rm "${net}" >/dev/null
  echo "removed labelled network ${net}"
done

volumes=$(docker volume ls -q --filter "label=${LABEL}")
for vol in ${volumes}; do
  docker volume rm -f "${vol}" >/dev/null
  echo "removed labelled volume ${vol}"
done

left_c=$(docker ps -aq --filter "label=${LABEL}" | wc -l | tr -d ' ')
left_n=$(docker network ls -q --filter "label=${LABEL}" | wc -l | tr -d ' ')
left_v=$(docker volume ls -q --filter "label=${LABEL}" | wc -l | tr -d ' ')
echo "labelled resources remaining: containers=${left_c} networks=${left_n} volumes=${left_v}"
[ "${left_c}${left_n}${left_v}" = "000" ]
