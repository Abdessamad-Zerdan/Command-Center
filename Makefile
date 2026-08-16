.PHONY: auth run test

auth:
	uv run python -m command_center.auth

run:
	uv run uvicorn command_center.app:app --reload --port 8000

test:
	uv run pytest -q
