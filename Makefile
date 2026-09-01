SHELL := /usr/bin/env bash
.DEFAULT_GOAL := help

UV    ?= uv
RUFF  ?= ruff
PY    ?= python
CAM   := camera

.PHONY: help sync lint format check test build clean preflight deps.check run install logs gain

help: ## Show targets
	@grep -E '^[a-zA-Z0-9_.-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

sync: ## Install/sync deps (including dev group)
	cd $(CAM) && $(UV) sync --dev

format: ## Format code
	cd $(CAM) && $(UV) run $(RUFF) format .

check: ## Lint (no fixes)
	cd $(CAM) && $(UV) run $(RUFF) check .

lint: ## Format + lint with fixes
	cd $(CAM) && $(UV) run $(RUFF) format .
	cd $(CAM) && $(UV) run $(RUFF) check . --fix

test: ## Run tests
	cd $(CAM) && $(UV) run pytest -q

build: ## Build sdist/wheel
	cd $(CAM) && $(UV) build

clean: ## Remove build artifacts
	rm -rf $(CAM)/dist $(CAM)/build $(CAM)/*.egg-info

preflight: ## Build + run twine metadata checks
	cd $(CAM) && $(UV) build
	cd $(CAM) && $(UV) tool run twine check dist/*

deps.check: ## Check for dependency issues
	cd $(CAM) && $(UV) run deptry .

run: ## Run camera preview in the foreground (Ctrl+C to stop)
	cd $(CAM) && $(UV) run main.py

install: ## Install as a systemd --user service (loopback mode, resource-capped)
	mkdir -p ~/code/ov02c10-camera-fix
	cp -r $(CAM) ~/code/ov02c10-camera-fix/
	cd ~/code/ov02c10-camera-fix/camera && $(UV) sync
	mkdir -p ~/.config/systemd/user/ov02c10-camera.service.d
	cp systemd/ov02c10-camera.service ~/.config/systemd/user/
	cp systemd/override.conf ~/.config/systemd/user/ov02c10-camera.service.d/
	systemctl --user daemon-reload
	systemctl --user enable --now ov02c10-camera

logs: ## Tail the running service's logs
	journalctl --user -u ov02c10-camera -f

gain: ## Print current sensor exposure/gain control values
	v4l2-ctl -d "$$(media-ctl -d /dev/media0 -e "$$(media-ctl -d /dev/media0 -p | grep -oE 'ov02c10 [0-9]+-[0-9a-f]{4}')")" -l
