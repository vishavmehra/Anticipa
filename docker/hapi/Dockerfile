# hapiproject/hapi is distroless (no shell, curl or wget), so a compose healthcheck cannot run in it.
# Copy a static busybox (musl) in solely to provide `wget` for the healthcheck.
# Both tags were verified by pulling on 2026-11-06.
FROM busybox:1.37.0-musl AS bb

FROM hapiproject/hapi:v8.12.0-2
COPY --from=bb /bin/busybox /busybox
