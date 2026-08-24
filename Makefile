ifneq ($(strip $(SERVICEGEN_DEPENDENCY_PROXY_DIR)),)
SERVICEGEN_DEPENDENCY_PROXY_HOST ?= localhost
SERVICEGEN_DEPENDENCY_PROXY_DOCKER_HOST ?= host.docker.internal
SERVICEGEN_NEXUS_PORT ?= 18081
SERVICEGEN_GIT_MIRROR_PORT ?= 18084
SERVICEGEN_DEPENDENCY_PROXY_BASE := http://$(SERVICEGEN_DEPENDENCY_PROXY_HOST):$(SERVICEGEN_NEXUS_PORT)/repository
SERVICEGEN_GIT_MIRROR_BASE := http://$(SERVICEGEN_DEPENDENCY_PROXY_HOST):$(SERVICEGEN_GIT_MIRROR_PORT)/cgi-bin/git
export GOPROXY := $(SERVICEGEN_DEPENDENCY_PROXY_BASE)/go-proxy/
export GOSUMDB := off
export NPM_CONFIG_REGISTRY := $(SERVICEGEN_DEPENDENCY_PROXY_BASE)/npm-proxy/
export PIP_INDEX_URL := $(SERVICEGEN_DEPENDENCY_PROXY_BASE)/pypi-proxy/simple
export PIP_TRUSTED_HOST := $(SERVICEGEN_DEPENDENCY_PROXY_HOST)
export UV_INDEX_URL := $(SERVICEGEN_DEPENDENCY_PROXY_BASE)/pypi-proxy/simple
export CARGO_REGISTRIES_CRATES_IO_INDEX := sparse+$(SERVICEGEN_DEPENDENCY_PROXY_BASE)/cargo-proxy/
export SERVICEGEN_GITHUB_RAW_URL := $(SERVICEGEN_DEPENDENCY_PROXY_BASE)/github-raw
export SERVICEGEN_GITLAB_RAW_URL := $(SERVICEGEN_DEPENDENCY_PROXY_BASE)/gitlab-raw
export SERVICEGEN_GIT_MIRROR_URL := $(SERVICEGEN_GIT_MIRROR_BASE)
export GIT_CONFIG_COUNT := 2
export GIT_CONFIG_KEY_0 := url.$(SERVICEGEN_GIT_MIRROR_BASE)/github.com/.insteadOf
export GIT_CONFIG_VALUE_0 := https://github.com/
export GIT_CONFIG_KEY_1 := url.$(SERVICEGEN_GIT_MIRROR_BASE)/gitlab.com/.insteadOf
export GIT_CONFIG_VALUE_1 := https://gitlab.com/
ifeq ($(origin SERVICEGEN_REAL_DOCKER),undefined)
SERVICEGEN_REAL_DOCKER := $(shell command -v docker)
endif
export SERVICEGEN_REAL_DOCKER
export PATH := $(CURDIR)/scripts/dependency-proxy-bin:$(PATH)
endif

.PHONY: profile durable durable-quick test clean dependency-source-cache-invalidate

profile:
	python3 examples/run.py $(ARGS)

durable:
	python3 examples/durable.py $(ARGS)

durable-quick:
	python3 examples/durable.py --skip-build --duration 5 --jobs 100 $(ARGS)

test:
	python3 -m unittest discover -s examples -p 'test_*.py' -v

clean:
	python3 examples/run.py --clean

dependency-source-cache-invalidate:
	@set -e; found=0; \
	for project in .dependencies/cppexample .dependencies/cppboostexample; do \
		if [ -f "$$project/make.generated.mk" ]; then \
			found=1; $(MAKE) -C "$$project" dependency-source-cache-invalidate; \
		fi; \
	done; \
	if [ "$$found" -eq 0 ]; then \
		echo "[dependency-source-cache] no fetched C++ examples; nothing to invalidate"; \
	fi
