.DEFAULT_GOAL := help

.PHONY: help install test bench lint format typecheck check clean build publish-test publish

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

install: ## Install dev dependencies
	uv sync

test: ## Run tests
	uv run python -m pytest --ignore=tests/benchmarks

bench: ## Run benchmarks
	uv run python -m pytest tests/benchmarks/ --benchmark-only

lint: ## Run linter and format check
	uv run ruff check .
	uv run ruff format --check .

format: ## Format code
	uv run ruff format .

typecheck: ## Run type checker (mypy strict)
	uv run python -m mypy src/

check: lint typecheck test ## Run lint + typecheck + tests

clean: ## Remove build artifacts and caches
	rm -rf dist/ build/ src/*.egg-info .mypy_cache .pytest_cache .ruff_cache
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true

build: clean ## Build sdist and wheel
	uv build

publish-test: build ## Publish to TestPyPI
	uv publish --publish-url https://test.pypi.org/legacy/

publish: build ## Publish to PyPI
	uv publish
