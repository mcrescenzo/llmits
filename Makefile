PY ?= python3

export PYTHONPATH := src$(if $(PYTHONPATH),:$(PYTHONPATH),)

.PHONY: test check lint typecheck format build release-check public-history clean

test:
	$(PY) -m unittest discover -s tests -v

check:
	$(PY) -m compileall -q src
	$(PY) -m unittest discover -s tests

lint:
	ruff check src

typecheck:
	mypy

format:
	ruff format src

build:
	$(PY) tools/package.py --output dist/llmits

release-check: check lint typecheck build
	$(PY) -m unittest tests.test_packaging -v

public-history:
	@test -n "$(OUTPUT)" || (echo "usage: make public-history OUTPUT=/path/outside/repository" >&2; exit 2)
	$(PY) tools/public_history.py --output "$(OUTPUT)"

clean:
	rm -rf dist
	find src tests -name __pycache__ -type d -exec rm -rf {} +
