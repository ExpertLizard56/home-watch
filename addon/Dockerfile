FROM python:3.13-alpine
RUN apk add --no-cache tzdata
COPY *.py monitors.json page.html /
# With the watchdog on, Home Assistant restarts Home Watch when this check fails (GET /health), not only when it exits.
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \n  CMD wget -q -O /dev/null http://127.0.0.1:8790/health || exit 1
CMD ["python", "-u", "/watch.py"]
