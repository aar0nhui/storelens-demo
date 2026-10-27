# Shortcuts. DEMO is the dataset the public demo is built from (a 90-minute simulated week pair, ~3 MB).
DEMO ?= demo_data
LABEL ?= Demo · simulated data

.PHONY: test test-all site serve demo-data fieldcheck-example backtest cloud-test

test:            ## fast tests (skips the minute-long video tests)
	python -m pytest -m "not slow"

test-all:        ## everything: fast tests, the slow video tests, the Worker tests (~7 min)
	python -m pytest
	$(MAKE) cloud-test

cloud-test:      ## private-app Worker tests (needs Node 20+ and `npm ci` in cloud/)
	cd cloud && npm test

fieldcheck-example: ## worked example of `storelens fieldcheck` from a demo folder (FROM=demo_out)
	scripts/run_fieldcheck_example.sh $(or $(FROM),demo_out) examples/fieldcheck

backtest:        ## internal-consistency check of the recommender on simulated worlds
	python scripts/backtest_recommender.py

site:            ## rebuild ./site from $(DEMO); commit the result to publish it
	storelens site --config $(DEMO)/store.yaml --db $(DEMO)/demo.db --out site --demo-label "$(LABEL)"

serve:           ## live dashboard (API mode) on http://127.0.0.1:8000
	storelens serve --config $(DEMO)/store.yaml --db $(DEMO)/demo.db

demo-data:       ## regenerate $(DEMO) from scratch: simulate + run the pipeline (several minutes, ~150 MB of temp video)
	storelens demo --out demo_out --minutes 90
	cp demo_out/store.yaml demo_out/demo.db $(DEMO)/ && cp demo_out/live/* $(DEMO)/live/
