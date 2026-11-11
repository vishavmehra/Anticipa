-- Runs once, on first initialisation of the postgres data volume (docker-entrypoint-initdb.d).
-- The main application database (POSTGRES_DB) is created by the image itself; its schema is owned by Alembic.
-- HAPI FHIR keeps its own database so its auto-managed schema never mixes with ours.
-- If the volume already exists without it:  docker compose exec postgres psql -U anticipa -d anticipa -c "CREATE DATABASE hapi"
SELECT 'CREATE DATABASE hapi'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'hapi')\gexec
