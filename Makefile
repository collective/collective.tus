.PHONY: help install test coverage lint format clean

VENV := .venv
BIN := $(VENV)/bin
PYTHON ?= python3
PACKAGE := collective.tus
PACKAGE_DIR := src/collective/tus

help:
	@echo "install   Create $(VENV) and install the package with the test and lint extras"
	@echo "test      Run the tests"
	@echo "coverage  Run the tests under coverage"
	@echo "lint      Run flake8, black, isort and zpretty checks"
	@echo "format    Format with black, isort and zpretty"

$(BIN)/python:
	$(PYTHON) -m venv $(VENV)
	$(BIN)/pip install --upgrade pip

install: $(BIN)/python
	$(BIN)/pip install -e ".[test,lint]"
	$(BIN)/pip install 'setuptools<81'  # Plone dependencies still import pkg_resources

test:
	$(BIN)/zope-testrunner --test-path=src -m $(PACKAGE)

coverage:
	$(BIN)/coverage run $(BIN)/zope-testrunner --test-path=src -m $(PACKAGE)
	$(BIN)/coverage report

lint:
	$(BIN)/flake8 $(PACKAGE_DIR)
	$(BIN)/black --check $(PACKAGE_DIR)
	$(BIN)/isort --check-only $(PACKAGE_DIR)
	$(BIN)/zpretty --check $(shell find $(PACKAGE_DIR) -name '*.zcml' -o -name '*.xml')

format:
	$(BIN)/black $(PACKAGE_DIR)
	$(BIN)/isort $(PACKAGE_DIR)
	$(BIN)/zpretty -i $(shell find $(PACKAGE_DIR) -name '*.zcml' -o -name '*.xml')

clean:
	rm -rf .coverage htmlcov build dist
	find . -type d -name __pycache__ -not -path "./.venv*" -exec rm -rf {} +
