# Makefile for arch-sidecar (Devrith Engine)
# Universal Architectural Co-Processor & Context Decoupling Engine

.PHONY: all default install update test audit clean help

default: install
all: install

install:
	@echo "Installing arch-sidecar in editable mode..."
	python3 -m pip install -e . || python3 -m pip install --break-system-packages -e . || python3 -m pip install --no-build-isolation -e .

update:
	@echo "Pulling latest changes from origin main..."
	git pull --ff-only origin main || git pull --ff-only
	@echo "Re-installing arch-sidecar..."
	python3 -m pip install -e . || python3 -m pip install --break-system-packages -e . || python3 -m pip install --no-build-isolation -e .

test:
	pytest -q

audit:
	arch-sidecar audit .

clean:
	@echo "Removing build, dist, egg-info, and pytest cache artifacts..."
	rm -rf build/ dist/ *.egg-info .pytest_cache/ .coverage htmlcov/
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete 2>/dev/null || true

help:
	@echo "Available arch-sidecar targets:"
	@echo "  make install - Install editable package locally"
	@echo "  make update  - Pull latest from origin main and re-install"
	@echo "  make test    - Run pytest suite (-q)"
	@echo "  make audit   - Run arch-sidecar quality and complexity audit"
	@echo "  make clean   - Remove build, dist, egg-info, and pytest cache artifacts"
