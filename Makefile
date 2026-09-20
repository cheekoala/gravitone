# Convenience wrappers. `make install` is the same thing install.sh does.
PYTHON ?= python3

.PHONY: help install uninstall dev test lint ui run docs clean

help:
	@echo "make install    install for the current user (pipx or a venv)"
	@echo "make uninstall  remove it again"
	@echo "make dev        editable install + test tools in ./.venv"
	@echo "make test       run the test suite"
	@echo "make ui         run the control panel from this checkout"
	@echo "make run        play from this checkout"
	@echo "make docs       redraw the pictures in the README"

install:
	./install.sh

uninstall:
	./install.sh --uninstall

dev:
	$(PYTHON) -m venv .venv
	./.venv/bin/python -m pip install --upgrade pip
	./.venv/bin/python -m pip install -e . pytest
	@echo "activate with: . .venv/bin/activate"

test:
	$(PYTHON) -m pytest -q

ui:
	$(PYTHON) -m gravitone ui

run:
	$(PYTHON) -m gravitone play

docs:
	$(PYTHON) tools/docshots.py

clean:
	rm -rf build dist *.egg-info .pytest_cache .venv
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
