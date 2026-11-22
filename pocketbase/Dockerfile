# PocketBase has no official Docker image, so we fetch a pinned release binary and
# verify its sha256 (digests taken from the GitHub release API for v0.40.6).
# NOTE: upstream announced future releases will be hosted off GitHub. To upgrade,
# bump PB_VERSION and both checksums after verifying them from the new host.
FROM alpine:3.21 AS fetch
ARG PB_VERSION=0.40.6
ARG TARGETARCH
RUN apk add --no-cache ca-certificates wget unzip
RUN case "${TARGETARCH}" in \
      amd64) SHA=0e1f609158ed945f809f8a02ae012aa0427129c66ebb63c1aee76f2c0309373f ;; \
      arm64) SHA=d22c66ceb67ca92b7d767f36d826e848a276bcfa3c197ead20bf87c42208984f ;; \
      *) echo "unsupported architecture: ${TARGETARCH}" >&2; exit 1 ;; \
    esac \
 && wget -q -O /tmp/pb.zip "https://github.com/pocketbase/pocketbase/releases/download/v${PB_VERSION}/pocketbase_${PB_VERSION}_linux_${TARGETARCH}.zip" \
 && echo "${SHA}  /tmp/pb.zip" | sha256sum -c - \
 && unzip -q /tmp/pb.zip pocketbase -d /tmp

FROM alpine:3.21
RUN apk add --no-cache ca-certificates wget \
 && adduser -D -u 10002 pb \
 && mkdir -p /pb/pb_data \
 && chown -R pb:pb /pb
COPY --from=fetch /tmp/pocketbase /usr/local/bin/pocketbase
COPY --chown=pb:pb pb_migrations /pb/pb_migrations
USER pb
WORKDIR /pb
EXPOSE 8090
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=5 \
  CMD wget -q -O /dev/null http://127.0.0.1:8090/api/health || exit 1
CMD ["pocketbase", "serve", "--http=0.0.0.0:8090", "--dir=/pb/pb_data", "--migrationsDir=/pb/pb_migrations"]
