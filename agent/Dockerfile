FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin argus

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# agent/slo.py imports collector/config.py directly so the agent always
# reasons against the same SLO thresholds the collector classifies with.
# Only config.py is imported (pure dataclasses) — the pollers and their DB
# drivers are never loaded in this image.
COPY collector/ ./collector/
COPY agent/ ./agent/

# Episodic memory (tier 2). Mounted as a volume/PVC in real deployments;
# created here so an unmounted run still starts instead of failing on write.
RUN mkdir -p /var/lib/argus/memory && chown -R argus:argus /var/lib/argus

USER argus

ENTRYPOINT ["python", "-m", "agent.main"]
CMD ["watch"]
