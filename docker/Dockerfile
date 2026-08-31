# syntax=docker/dockerfile:1

ARG PYTHON_VERSION=3.13
ARG UV_VERSION=0.11.6

FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv

FROM python:${PYTHON_VERSION}-slim-trixie AS builder

ENV UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv

WORKDIR /src

COPY --from=uv /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src

FROM builder AS builder-postgres

RUN uv sync --frozen --no-dev --no-editable \
    --no-install-package mysql-connector-python


FROM python:${PYTHON_VERSION}-slim-trixie AS runtime-base

ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN groupadd --gid 10001 db-condenser \
    && useradd --uid 10001 --gid db-condenser \
        --home-dir /home/db-condenser --create-home \
        --shell /usr/sbin/nologin db-condenser \
    && install -d --owner=db-condenser --group=db-condenser /work

WORKDIR /work
ENTRYPOINT ["subset"]


FROM runtime-base AS postgres-client-source

ARG POSTGRES_MAJOR=18

RUN apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates curl \
    && install -d /usr/share/postgresql-common/pgdg \
    && curl --fail --show-error --silent \
        https://www.postgresql.org/media/keys/ACCC4CF8.asc \
        --output /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
    && echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt trixie-pgdg main" \
        > /etc/apt/sources.list.d/pgdg.list \
    && apt-get update \
    && apt-get install --yes --no-install-recommends \
        "postgresql-client-${POSTGRES_MAJOR}" \
    && apt-get purge --yes --auto-remove curl \
    && rm -rf /var/lib/apt/lists/* \
    && install -d /opt/db-clients \
    && install "/usr/lib/postgresql/${POSTGRES_MAJOR}/bin/pg_dump" \
        /opt/db-clients/pg_dump \
    && install "/usr/lib/postgresql/${POSTGRES_MAJOR}/bin/psql" \
        /opt/db-clients/psql


FROM runtime-base AS postgres-packages

RUN apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates curl \
    && install -d /usr/share/postgresql-common/pgdg \
    && curl --fail --show-error --silent \
        https://www.postgresql.org/media/keys/ACCC4CF8.asc \
        --output /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
    && echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt trixie-pgdg main" \
        > /etc/apt/sources.list.d/pgdg.list \
    && apt-get update \
    && apt-get install --yes --no-install-recommends \
        liblz4-1 libpq5 libreadline8t64 libxxhash0 libzstd1 \
    && apt-get purge --yes --auto-remove curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=postgres-client-source /opt/db-clients/ /usr/local/bin/


FROM postgres-packages AS postgres

COPY --from=builder-postgres /opt/venv /opt/venv

USER db-condenser


FROM builder AS builder-mysql

RUN uv sync --frozen --no-dev --no-editable \
    --no-install-package psycopg \
    --no-install-package psycopg-binary


FROM runtime-base AS mysql-client-source

RUN apt-get update \
    && apt-get install --yes --no-install-recommends default-mysql-client \
    && rm -rf /var/lib/apt/lists/* \
    && install -d /opt/db-clients \
    && install /usr/bin/mariadb /opt/db-clients/mysql \
    && install /usr/bin/mariadb-dump /opt/db-clients/mysqldump


FROM runtime-base AS mysql-packages

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        libedit2 libmariadb3 libncurses6 libstdc++6 libzstd1 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=mysql-client-source /opt/db-clients/ /usr/local/bin/


FROM mysql-packages AS mysql

COPY --from=builder-mysql /opt/venv /opt/venv

USER db-condenser


FROM builder AS builder-full

RUN uv sync --frozen --no-dev --no-editable


FROM postgres-packages AS full-packages

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        libedit2 libmariadb3 libncurses6 libstdc++6 libzstd1 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=mysql-client-source /opt/db-clients/ /usr/local/bin/


FROM full-packages AS full

COPY --from=builder-full /opt/venv /opt/venv

USER db-condenser
