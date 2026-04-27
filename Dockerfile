FROM python:3.12-slim AS builder

WORKDIR /build
RUN pip install --no-cache-dir build

COPY pyproject.toml README.md ./
COPY src ./src

RUN python -m build --wheel --outdir /dist


FROM python:3.12-slim

LABEL org.opencontainers.image.title="shakedown"
LABEL org.opencontainers.image.description="Durable mirroring of public music archives onto a local NAS."

RUN useradd -u 1000 -m shakedown
WORKDIR /home/shakedown

COPY --from=builder /dist/*.whl /tmp/
RUN pip install --no-cache-dir /tmp/*.whl[serve] && rm -f /tmp/*.whl

USER shakedown

# /config holds shakedown.yaml; /data holds archive + library.
VOLUME ["/config", "/data"]

ENV SHAKEDOWN_CONFIG=/config/shakedown.yaml

ENTRYPOINT ["shakedown", "--config", "/config/shakedown.yaml"]
CMD ["--help"]
