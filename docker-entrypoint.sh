#!/bin/sh
# Container entrypoint: apply the database migrations, then exec the command (the app by default).
#
# A failed migration (for example PostgreSQL not up yet) is logged by the migrate command, by
# exception class only, and does not stop the container: the service must start and answer
# liveness and readiness without its database (FR-011, FR-015). Run the migrate command again,
# or restart the container, once PostgreSQL is reachable.
set -u

if ! python -m ecommerce_ai migrate; then
    echo "docker-entrypoint: migrations not applied; starting the service anyway" >&2
fi

exec "$@"
