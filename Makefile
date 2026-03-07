.PHONY: dev test lint format clean

dev:
	pip install -e ".[dev]"

test:
	pytest tests/ -v

test-all:
	pytest -m '' -v

lint:
	ruff check intaris/ tests/

format:
	ruff format intaris/ tests/

clean:
	rm -rf build/ dist/ *.egg-info/ .pytest_cache/ .ruff_cache/
	find . -type d -name __pycache__ -exec rm -rf {} +
