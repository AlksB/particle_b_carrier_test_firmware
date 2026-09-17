# build context is dashboard/, so the schema file can come along for the
# startup migration (see migrate() in ingest.py)
FROM python:3.12-slim
RUN pip install --no-cache-dir "psycopg[binary]==3.2.*"
COPY ingest/ingest.py /ingest.py
COPY postgres/init/01_schema.sql /schema.sql
ENV SCHEMA_SQL=/schema.sql
ENTRYPOINT ["python3", "-u", "/ingest.py"]
