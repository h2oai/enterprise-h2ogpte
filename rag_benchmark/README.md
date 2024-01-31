## Run h2oGPTe End-to-End RAG Benchmark Suite for different LLMs

Run the end-to-end benchmark for RAG, across all supported LLMs (currently ~17 different ones), with ~120 problems. Takes less than 2h.

### How to reproduce the RAG Benchmarks using the h2oGPTe client

First, get your API key from https://h2ogpte.genai.h2o.ai/settings, then export it in your environment:

```
# export H2OGPTE_TEST_ADDRESS=https://h2ogpte.genai.h2o.ai  # point to h2oGPTe instance
export H2OGPTE_API_KEY=sk-XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX   # your API key
```

Second, set up the Python client and test environment (Python 3.8+ recommended)
```
git clone git@github.com:h2oai/enterprise-h2ogpte.git
cd enterprise-h2ogpte
cd rag_benchmark
make setup
```

Third, run the benchmarks (token limits may apply, based on where h2oGPTe is running).
You can edit the [test code](test_benchmarks.py) to limit the set of LLMs (GPT-4/Claude are expensive and disabled by default).
And here are the input documents, questions and expected answers: [Q&A dataset](e2e_df.csv).
```
make benchmark
```

Finally, look at [the Benchmark results](results/test_client_e2e.md).
