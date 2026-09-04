FROM python:3.12-slim

ARG PIP_INDEX_URL=https://pypi.org/simple
ARG PIP_EXTRA_INDEX_URL=
ARG APP_UID=1000
ARG APP_GID=1000

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_DEFAULT_TIMEOUT=120 \
    PIP_RETRIES=10

WORKDIR /app

COPY requirements.txt .
RUN PIP_INDEX_URL="${PIP_INDEX_URL}" PIP_EXTRA_INDEX_URL="${PIP_EXTRA_INDEX_URL}" \
    python -m pip install --no-cache-dir --prefer-binary --timeout 120 --retries 10 -r requirements.txt

RUN groupadd --gid "${APP_GID}" app && \
    useradd --uid "${APP_UID}" --gid app --home-dir /app --no-create-home --shell /usr/sbin/nologin app

COPY --chown=app:app . .
RUN mkdir -p /app/data && chown app:app /app/data

USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).read()" || exit 1

CMD ["gunicorn", "app.main:app", "-k", "uvicorn.workers.UvicornWorker", "-w", "1", "-b", "0.0.0.0:8000", "--timeout", "60", "--graceful-timeout", "30", "--keep-alive", "5", "--access-logfile", "-", "--error-logfile", "-", "--capture-output"]
