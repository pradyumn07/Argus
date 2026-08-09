FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin argus

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# loadgen/generate.py does:
#   sys.path.insert(0, Path(__file__).resolve().parent.parent / "collector")
# i.e. it expects collector/ as a SIBLING directory one level up from itself,
# to import fleet.py and reuse the exact same fleet definition the collector
# uses. Preserve that layout instead of flattening it, so the import keeps
# working unmodified: /app/loadgen/generate.py + /app/collector/fleet.py.
COPY collector/ ./collector/
COPY loadgen/ ./loadgen/

WORKDIR /app/loadgen

USER argus

ENTRYPOINT ["python", "generate.py"]
CMD ["baseline"]
