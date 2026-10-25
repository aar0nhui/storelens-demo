# Shortcuts. DEMO is the dataset the public demo is built from (a 90-minute simulated week pair, ~3 MB).
DEMO ?= demo_data
LABEL ?= Demo · simulated data

.PHONY: test site serve demo-data

test:            ## fast tests (skips the minute-long video tests)
	python -m pytest -m "not slow"

site:            ## rebuild ./site from $(DEMO); commit the result to publish it
	storelens site --config $(DEMO)/store.yaml --db $(DEMO)/demo.db --out site --demo-label "$(LABEL)"

serve:           ## live dashboard (API mode) on http://127.0.0.1:8000
	storelens serve --config $(DEMO)/store.yaml --db $(DEMO)/demo.db

demo-data:       ## regenerate $(DEMO) from scratch: simulate + run the pipeline (several minutes, ~150 MB of temp video)
	storelens demo --out demo_out --minutes 90
	cp demo_out/store.yaml demo_out/demo.db $(DEMO)/ && cp demo_out/live/* $(DEMO)/live/
