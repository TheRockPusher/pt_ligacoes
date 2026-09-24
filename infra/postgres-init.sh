#!/bin/sh
set -eu
# Only runs on first initialization of the local development volume.
psql --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" --set ON_ERROR_STOP=1 \
  --command 'CREATE DATABASE pt_ligacoes_e2e;'
