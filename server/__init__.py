"""The BizData MCP server.

Four read tools over the synthetic consultancy in db/, aggregating in SQL so that forty
thousand time entries come back as tens of rows. Run it locally with

    uvicorn server.app:app

from the repository root. Steps 1 through 4 run against the Docker Postgres in
docker-compose.yml; Aurora and the RDS Data API land at step 5, behind the same
``BIZDATA_DB_BACKEND`` switch scripts/writers.py already uses for loading.
"""
