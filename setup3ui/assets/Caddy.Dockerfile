FROM caddy:2.11.7-builder AS builder
RUN xcaddy build v2.11.7 --with github.com/mholt/caddy-l4@v0.1.3 \
    && mkdir -p /licenses \
    && cp "$(go env GOMODCACHE)/github.com/caddyserver/caddy/v2@v2.11.7/LICENSE" /licenses/Caddy-LICENSE \
    && cp "$(go env GOMODCACHE)/github.com/mholt/caddy-l4@v0.1.3/LICENSE" /licenses/Caddy-Layer4-LICENSE

FROM caddy:2.11.7
COPY --from=builder /usr/bin/caddy /usr/bin/caddy
COPY --from=builder /licenses/ /usr/share/licenses/caddy/
