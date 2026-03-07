.PHONY: dev test test-all lint format css css-watch clean

# Install project in editable mode with dev dependencies.
dev:
	uv pip install -e ".[dev]"

# Unit tests (fast, no API key needed).
test:
	uv run pytest tests/ -v

# All tests including e2e (requires LLM_API_KEY).
test-all:
	uv run pytest -m '' -v

# Lint check.
lint:
	uv run ruff check intaris/ tests/

# Auto-format.
format:
	uv run ruff format intaris/ tests/

# Build minified Tailwind CSS.
css:
	npx tailwindcss -i intaris/ui/src/input.css -o intaris/ui/static/css/app.css --minify

# Watch mode — rebuilds CSS on file changes during development.
css-watch:
	npx tailwindcss -i intaris/ui/src/input.css -o intaris/ui/static/css/app.css --watch

# Remove build artifacts and caches.
clean:
	rm -rf build/ dist/ *.egg-info/ .pytest_cache/ .ruff_cache/
	find . -type d -name __pycache__ -exec rm -rf {} +
