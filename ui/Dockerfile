FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin argus

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# The UI imports the agent (which imports collector/config.py for real SLO
# thresholds), so all three ship together.
COPY collector/ ./collector/
COPY agent/ ./agent/
COPY ui/ ./ui/

RUN mkdir -p /var/lib/argus/memory && chown -R argus:argus /var/lib/argus

EXPOSE 8080
USER argus

CMD ["python", "-m", "ui.server"]
