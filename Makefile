.PHONY: install dev run lint fmt test docker

install:
	pip install -e ".[dev,tokens]"

run:
	uvicorn app.main:app --host 0.0.0.0 --port 8000 --no-access-log

dev:
	uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

lint:
	ruff check app tests

fmt:
	ruff check --fix app tests

test:
	pytest -q

docker:
	docker compose up --build
