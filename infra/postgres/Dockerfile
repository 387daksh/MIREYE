FROM pgvector/pgvector:0.8.1-pg16-bookworm AS vector
FROM postgis/postgis:16-3.5

COPY --from=vector /usr/lib/postgresql/16/lib/vector.so /usr/lib/postgresql/16/lib/vector.so
COPY --from=vector /usr/share/postgresql/16/extension/vector* /usr/share/postgresql/16/extension/
