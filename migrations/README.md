# Database migration foundation

`20260825_0001` is a non-destructive baseline for the schema currently owned by
`WorkspaceStore.initialize()`. Existing databases are inspected and then stamped;
they are never upgraded by importing application modules.

```powershell
$env:DATABASE_URL = "sqlite:///C:/path/to/explicit/test.db"
alembic stamp 20260825_0001
alembic current
```

PostgreSQL/PostGIS adoption should first reproduce the current tables in a new
migration, backfill a copy of production data, validate hashes/counts, and only
then switch repository adapters. SQLite remains the test/demo adapter.
