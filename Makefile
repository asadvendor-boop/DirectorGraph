SHELL := /bin/bash

.PHONY: install test lint api worker web demo docker-up docker-down package
install:
	cd services/api && python -m pip install -e '.[dev]'
	cd apps/web && npm install

test:
	cd services/api && pytest -q
lint:
	cd services/api && ruff check app tests
	cd apps/web && npm run lint
api:
	cd services/api && uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
worker:
	cd services/api && python -m app.worker
web:
	cd apps/web && npm run dev -- --host 0.0.0.0
demo:
	cd services/api && python -m app.demo
docker-up:
	docker compose up --build
docker-down:
	docker compose down -v
package:
	zip -r directorgraph-source.zip . -x '.git/*' 'directorgraph-source.zip' '*/node_modules/*' 'node_modules/*' '*/.venv/*' '.venv/*' '*/data/*' 'data/*' '*/media/*' 'media/*' 'local-output/*'
