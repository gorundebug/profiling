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
