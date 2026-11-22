#!/bin/sh
set -eu

coverage_mode=${1:-}
if [ "$#" -gt 1 ] || { [ -n "$coverage_mode" ] && [ "$coverage_mode" != "--report-only" ]; }; then
    echo "Usage: $0 [--report-only]" >&2
    exit 2
fi

root=$(CDPATH='' cd -- "$(dirname -- "$0")/../.." && pwd)
daemon="setup3ui-test-$$"
scratch="$daemon-files"
report="$root/coverage-report"
mkdir -p "$report"

cleanup() {
    docker container rm -f -v "$daemon" >/dev/null 2>&1 || :
    docker volume rm "$scratch" >/dev/null 2>&1 || :
}
trap cleanup EXIT
trap 'exit 1' HUP INT TERM

docker volume create "$scratch" >/dev/null
docker run -d --privileged --name "$daemon" -v "$scratch:/sandbox" docker:28-dind \
    dockerd --host=unix:///sandbox/docker.sock --storage-driver=vfs >/dev/null

attempt=0
until docker exec "$daemon" docker --host unix:///sandbox/docker.sock info >/dev/null 2>&1; do
    attempt=$((attempt + 1))
    if [ "$attempt" -ge 60 ]; then
        docker logs "$daemon" >&2
        exit 1
    fi
    sleep 1
done

# Reuse local copies when available. The private daemon can pull any missing image.
set --
for image in ghcr.io/mhsanaei/3x-ui:latest ghcr.io/mhsanaei/3x-ui:2.8.11 \
    setup-3x-ui-caddy:2.11.7-l4 certbot/certbot:v5.1.0 ghcr.io/letsencrypt/pebble:latest python:3.13-slim-trixie; do
    if docker image inspect "$image" >/dev/null 2>&1; then
        set -- "$@" "$image"
    fi
done
if [ "$#" -gt 0 ]; then
    docker image save "$@" | docker exec -i "$daemon" docker --host unix:///sandbox/docker.sock load
fi

run() {
    runner=$1
    shift
    docker run --rm --network "container:$daemon" -v "$root:/work:ro" -v "$scratch:/sandbox" \
        -v "$report:/report" -e "SUITE=$runner" -e PYTHONDONTWRITEBYTECODE=1 \
        -e DOCKER_HOST=unix:///sandbox/docker.sock "setup3ui-test:$runner" "$@"
}

status=0
for suite in bookworm trixie; do
    docker build --build-arg "DEBIAN=$suite" -t "setup3ui-test:$suite" -f "$root/tests/sandbox/Dockerfile" "$root/tests/sandbox"
    run "$suite" sh -ec '
        ln -sf /sandbox/docker.sock /var/run/docker.sock
        mkdir -p "/sandbox/$SUITE" "/sandbox/$SUITE/tmp"
        export TMPDIR="/sandbox/$SUITE/tmp" COVERAGE_FILE="/sandbox/$SUITE/.coverage"
        status=0
        for group in tests tests/integration; do
            python3 -m coverage run --rcfile=tests/sandbox/.coveragerc -m unittest discover -s "$group" -v || status=1
        done
        COVERAGE_PROCESS_START=/work/tests/sandbox/.coveragerc \
            python3 -m unittest discover -s tests/functional -v || status=1
        exit "$status"
    ' || status=1
done

run trixie sh -ec '
    export COVERAGE_FILE=/sandbox/.coverage
    python3 -m coverage combine --rcfile=tests/sandbox/.coveragerc /sandbox/bookworm /sandbox/trixie
    python3 -m coverage report --rcfile=tests/sandbox/.coveragerc > /report/coverage.txt
    python3 -m coverage json --rcfile=tests/sandbox/.coveragerc -o /report/coverage.json
    cat /report/coverage.txt
' || status=1
if [ "${coverage_mode:-}" != "--report-only" ]; then
    run trixie python3 tests/sandbox/report.py /report/coverage.json || status=1
fi
exit "$status"
