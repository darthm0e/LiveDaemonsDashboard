FROM python:3.12-slim

# iputils-ping wird für type: ping benötigt; curl nur für den Healthcheck.
RUN apt-get update \
    && apt-get install -y --no-install-recommends iputils-ping curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY config.yml /srv/config.default.yml
COPY entrypoint.sh /srv/entrypoint.sh
RUN chmod +x /srv/entrypoint.sh

ENV CONFIG_PATH=/config/config.yml
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=4s --start-period=5s \
    CMD curl -fsS http://127.0.0.1:8080/healthz || exit 1

ENTRYPOINT ["/srv/entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
