FROM docker:28-dind AS docker_cli

FROM debian:trixie-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends acl ca-certificates curl openssl python3 python3-coverage python3-yaml \
    && apt-get clean
RUN site="$(python3 -c 'from pathlib import Path; import coverage; print(Path(coverage.__file__).resolve().parents[1])')" \
    && mkdir -p "$site" \
    && printf '%s\n' 'import os; os.environ.get("COVERAGE_PROCESS_START") and __import__("coverage").process_startup()' > "$site/setup3ui_coverage.pth"
COPY --from=docker_cli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=docker_cli /usr/local/libexec/docker/cli-plugins/docker-compose /usr/local/libexec/docker/cli-plugins/docker-compose
WORKDIR /work
