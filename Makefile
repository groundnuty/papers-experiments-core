.PHONY: check test lint fmt clean hooks

# The definition of green: lint, unit tests, and the study-token check over src/.
check: lint test
	uv run python -m expcore.provenance.study_tokens check src/

test:
	uv run pytest

lint:
	uv run ruff check
	uv run ruff format --check

fmt:
	uv run ruff check --fix
	uv run ruff format

clean:
	rm -rf .pytest_cache .ruff_cache build dist
	find src tests -name __pycache__ -type d -prune -exec rm -rf {} +

# Install the pre-push hook that runs the study-token check over the commits being pushed.
hooks:
	install -m 755 scripts/pre-push "$$(git rev-parse --git-path hooks/pre-push)"
