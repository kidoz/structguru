.DEFAULT_GOAL := help

.PHONY: help install test bench lint format typecheck audit sbom python-check rust-check check clean build publish-test publish

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

install: ## Install dev dependencies and all integration extras
	uv sync --locked --all-extras

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

audit: ## Audit locked Python and Rust dependencies
	uv audit --locked
	cargo deny check advisories --locked

sbom: ## Generate CycloneDX SBOMs for Python and Rust dependencies
	mkdir -p dist
	uv export --quiet --preview-features sbom-export --locked --all-extras --no-dev --format cyclonedx1.5 --output-file dist/structguru-python.cdx.json
	uv run python scripts/generate_rust_sbom.py --output dist/structguru-rust.cdx.json

python-check: lint typecheck test ## Run all required Python source quality gates

rust-check: ## Run all required Rust source quality gates
	cargo fmt --all -- --check
	cargo check --workspace
	cargo test --workspace
	cargo clippy --workspace --lib -- -D warnings

check: python-check rust-check ## Run the complete Python and Rust quality gate

clean: ## Remove build artifacts and caches
	rm -rf dist/ build/ target/ src/*.egg-info .mypy_cache .pytest_cache .ruff_cache
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true

build: clean ## Build sdist and wheel
	uv build

publish-test: build ## Publish to TestPyPI
	uv publish --publish-url https://test.pypi.org/legacy/

publish: build ## Publish to PyPI
	uv publish
