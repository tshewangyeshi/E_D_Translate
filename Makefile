# All logic lives in tools/check.py so the same checks run on Windows
# (python tools/check.py) and in CI (make check).

PYTHON ?= python

.PHONY: check install test-py test-widget

install:
	$(PYTHON) -m pip install -e ".[dev]"
	cd adapters/widget && npm ci

check:
	$(PYTHON) tools/check.py

test-py:
	$(PYTHON) -m pytest

test-widget:
	cd adapters/widget && npx vitest run
