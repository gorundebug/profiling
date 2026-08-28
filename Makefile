ifneq ($(strip $(DEPENDENCY_PROXY_DIR)),)
DEPENDENCY_PROXY_HOST ?= localhost
DEPENDENCY_PROXY_DOCKER_HOST ?= host.docker.internal
DEPENDENCY_PROXY_PORT ?= 18081
DEPENDENCY_GIT_MIRROR_PORT ?= 18084
DEPENDENCY_PROXY_BASE := http://$(DEPENDENCY_PROXY_HOST):$(DEPENDENCY_PROXY_PORT)/repository
DEPENDENCY_GIT_MIRROR_BASE := http://$(DEPENDENCY_PROXY_HOST):$(DEPENDENCY_GIT_MIRROR_PORT)/cgi-bin/git
export GOPROXY := $(DEPENDENCY_PROXY_BASE)/go-proxy/
export GOSUMDB := off
export NPM_CONFIG_REGISTRY := $(DEPENDENCY_PROXY_BASE)/npm-proxy/
export PIP_INDEX_URL := $(DEPENDENCY_PROXY_BASE)/pypi-proxy/simple
export PIP_TRUSTED_HOST := $(DEPENDENCY_PROXY_HOST)
export UV_INDEX_URL := $(DEPENDENCY_PROXY_BASE)/pypi-proxy/simple
export CARGO_REGISTRIES_CRATES_IO_INDEX := sparse+$(DEPENDENCY_PROXY_BASE)/cargo-proxy/
export DEPENDENCY_GITHUB_RAW_URL := $(DEPENDENCY_PROXY_BASE)/github-raw
export DEPENDENCY_GITLAB_RAW_URL := $(DEPENDENCY_PROXY_BASE)/gitlab-raw
export DEPENDENCY_GIT_MIRROR_URL := $(DEPENDENCY_GIT_MIRROR_BASE)
export GIT_CONFIG_COUNT := 2
export GIT_CONFIG_KEY_0 := url.$(DEPENDENCY_GIT_MIRROR_BASE)/github.com/.insteadOf
export GIT_CONFIG_VALUE_0 := https://github.com/
export GIT_CONFIG_KEY_1 := url.$(DEPENDENCY_GIT_MIRROR_BASE)/gitlab.com/.insteadOf
export GIT_CONFIG_VALUE_1 := https://gitlab.com/
ifeq ($(origin DEPENDENCY_REAL_DOCKER),undefined)
DEPENDENCY_REAL_DOCKER := $(shell command -v docker)
endif
export DEPENDENCY_REAL_DOCKER
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
