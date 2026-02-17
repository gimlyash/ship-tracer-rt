FROM apache/superset:latest

USER root
RUN pip install --no-cache-dir --target=/app/.venv/lib/python3.10/site-packages psycopg2-binary
USER superset
