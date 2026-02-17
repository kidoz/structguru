.DEFAULT_GOAL := help

.PHONY: help install test lint format typecheck check clean build publish-test publish

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

install: ## Install dev dependencies
	uv sync

test: ## Run tests
	uv run pytest

lint: ## Run linter
	uv run ruff check .

format: ## Format code
	uv run ruff format .

typecheck: ## Run type checker (mypy strict)
	uv run mypy src/

check: lint typecheck test ## Run lint + typecheck + tests

clean: ## Remove build artifacts
	rm -rf dist/ build/ src/*.egg-info

build: clean ## Build sdist and wheel
	uv build

publish-test: build ## Publish to TestPyPI
	uv publish --publish-url https://test.pypi.org/legacy/

publish: build ## Publish to PyPI
	uv publish
