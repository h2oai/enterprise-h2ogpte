## Run h2oGPTe End-to-End RAG Benchmark Suite for different LLMs

Run the end-to-end benchmark for RAG, across all supported LLMs (currently ~17 different ones), with ~120 problems. Takes less than 2h.

First, get your API key from https://h2ogpte.genai.h2o.ai/settings, then enter it below

```
# export H2OGPTE_TEST_ADDRESS=https://h2ogpte.genai.h2o.ai  # point to h2oGPTe instance
export H2OGPTE_API_KEY=sk-XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX   # set your API token
```

Now, you can run the benchmarks (up to token limits of USD 10 per day).
You can edit the [test code](test_benchmarks.py) to limit the set of LLMs (GPT-4/Claude are expensive).
```
make setup
make benchmark
```
then look at [the Benchmark results](test_client_e2e.md).
