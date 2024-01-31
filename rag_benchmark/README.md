## Run h2oGPTe End-to-End RAG Benchmark Suite for different LLMs

Run the end-to-end benchmark for RAG, across all supported LLMs (currently ~17 different ones), with ~120 problems. Takes less than 2h.

First, get your API key from https://h2ogpte.genai.h2o.ai/settings, then export it in your environment:

```
# export H2OGPTE_TEST_ADDRESS=https://h2ogpte.genai.h2o.ai  # point to h2oGPTe instance
export H2OGPTE_API_KEY=sk-XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX   # your API key
```


Set up Python environment (Python 3.8+ recommended)
```
cd rag_benchmark
make setup
```

Now, you can run the benchmarks (up to token limits of USD 10 per day).
You can edit the [test code](rag_benchmark/test_benchmarks.py) to limit the set of LLMs (GPT-4/Claude are expensive and disabled by default).
```
make benchmark
```
then look at [the Benchmark results](rag_benchmark/results/test_client_e2e.md).
