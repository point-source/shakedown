# The dashboard page (§spec:dashboard-page). Built once on the build host's own
# platform: its output is static files, the same for every image platform. The
# runtime image gets only the files; Node never runs in it.
FROM --platform=$BUILDPLATFORM node:24-slim AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build


FROM python:3.12-slim AS builder

# Install from uv.lock so every build of a commit gets the same libraries.
COPY --from=ghcr.io/astral-sh/uv:0.13.7 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv

# The build context has no git history, so the version (normally read from the
# git tag) is passed in. CI sets it; a local build reports 0.0.0+local.
ARG SHAKEDOWN_VERSION=0.0.0+local
ENV SETUPTOOLS_SCM_PRETEND_VERSION=${SHAKEDOWN_VERSION}

WORKDIR /build
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-install-project --extra serve

COPY src ./src
# The page ships as package data, so it goes in before the project is installed.
COPY --from=web /web/dist/ ./src/shakedown/dashboard_web/
RUN uv sync --locked --no-editable --extra serve


FROM python:3.12-slim

LABEL org.opencontainers.image.title="shakedown"
LABEL org.opencontainers.image.description="Durable mirroring of public music archives onto a local NAS."

RUN useradd -u 1000 -U -m shakedown
WORKDIR /home/shakedown

# Same path as in the builder, so the venv's scripts keep working.
COPY --from=builder /opt/venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH

USER 1000:1000

# /config holds shakedown.yaml; /data holds archive + library.
VOLUME ["/config", "/data"]

ENV SHAKEDOWN_CONFIG=/config/shakedown.yaml

ENTRYPOINT ["shakedown", "--config", "/config/shakedown.yaml"]
CMD ["--help"]
