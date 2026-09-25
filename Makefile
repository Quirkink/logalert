.PHONY: setup test lint format typecheck check test-live build clean redis-up redis-down

setup:            ## install every extra plus dev dependencies
	uv sync --all-extras

test:
	uv run pytest -q

lint:
	uv run ruff check .
	uv run ruff format --check .

format:
	uv run ruff check --fix .
	uv run ruff format .

typecheck:
	uv run mypy src

check: lint typecheck test  ## what CI runs

test-live:        ## tests that need real Telegram/Matrix/Redis
	uv run pytest -q -m live

redis-up:         ## local Redis for the live tests and the worker
	docker compose up -d

redis-down:
	docker compose down

build:
	uv build
	uv run twine check dist/*

clean:
	rm -rf dist build .pytest_cache .mypy_cache .ruff_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
