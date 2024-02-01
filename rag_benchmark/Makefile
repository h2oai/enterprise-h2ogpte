PYTEST_BIN:=./venv/bin/python -u -m pytest
PYTEST_ARGS:=--color=yes --durations=20 -s -v --instafail --disable-pytest-warnings --timeout=3600
PYTEST_BASE=$(PYTEST_BIN) $(PYTEST_ARGS)

setup:
	rm -rf venv
	python3 -m venv venv
	./venv/bin/python -m pip install h2ogpte==1.3.4
	./venv/bin/python -m pip install -r requirements-pytest.txt
	. ./venv/bin/activate

benchmark:
	mkdir -p data/cached
	make test_client_e2e_ingest
	make test_client_e2e

test_client_e2e: export HARD_ASSERTS=1
test_client_e2e: export TEST_ALL=1
test_client_e2e: export DROP_EXPENSIVE=1
test_client_e2e: export NUM_PYTEST_PARALLEL=20
test_client_e2e:
	$(PYTEST_BASE) -n $(NUM_PYTEST_PARALLEL) -m e2e --junit-xml=test_client.xml test_benchmarks.py || $(PYTEST_BASE) test_benchmarks.py::test_pass_rate_e2e
	grep "##" test_client.txt | tail -n 1

test_client_e2e_ingest: export TEST_ALL=1
test_client_e2e_ingest: export DROP_EXPENSIVE=1
test_client_e2e_ingest: export INGEST_ONLY=1
test_client_e2e_ingest: export NUM_PYTEST_PARALLEL=20
test_client_e2e_ingest:
	$(PYTEST_BASE) -n $(NUM_PYTEST_PARALLEL) -m e2e test_benchmarks.py
