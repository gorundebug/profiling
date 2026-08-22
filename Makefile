.PHONY: profile test clean

profile:
	python3 examples/run.py $(ARGS)

test:
	python3 -m unittest discover -s examples -p 'test_*.py' -v

clean:
	python3 examples/run.py --clean
