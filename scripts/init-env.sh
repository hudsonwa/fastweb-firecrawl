#!/bin/sh
# Create .env from .env.example with freshly generated secrets. Refuses to overwrite an existing .env.
set -eu
cd "$(dirname "$0")/.."
if [ -e .env ]; then
  echo ".env already exists; leaving it alone" >&2
  exit 1
fi
gen() { openssl rand -hex "$1"; }
umask 077
sed -e "s/^SEARXNG_SECRET=.*/SEARXNG_SECRET=$(gen 32)/" \
    -e "s/^# FIRECRAWL_POSTGRES_PASSWORD=.*/# FIRECRAWL_POSTGRES_PASSWORD=$(gen 24)/" \
    -e "s/^# FIRECRAWL_BULL_AUTH_KEY=.*/# FIRECRAWL_BULL_AUTH_KEY=$(gen 24)/" .env.example > .env
echo "wrote .env (SEARXNG_SECRET set; Firecrawl secrets generated but commented out until you enable the profile)"
