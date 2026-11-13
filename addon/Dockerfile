FROM python:3.13-alpine
RUN apk add --no-cache tzdata
COPY *.py monitors.json page.html /
CMD ["python", "-u", "/watch.py"]
