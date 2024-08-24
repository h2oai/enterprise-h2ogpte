import ast
import json
import os
import subprocess
import time
from datetime import datetime
from typing import Union

import pytest

from h2ogpte import H2OGPTE
from conftest import e2e_data

assert e2e_data, "Must have Q&A RAG dataset."
try:
    # # For h2ogpte CI
    from mux_py.tests.test_mux import REMOTE_ADDRESS, API_KEY
    from parse.tests.datasets import CachedFile
except ModuleNotFoundError as e:
    # For open-source benchmarks repository
    from datasets import CachedFile

    REMOTE_ADDRESS = os.getenv("H2OGPTE_TEST_ADDRESS", "https://h2ogpte.genai.h2o.ai")
    API_KEY = os.getenv("H2OGPTE_API_KEY")
assert REMOTE_ADDRESS, "Must have h2oGPTe remote server address."
assert API_KEY, "Must set H2OGPTE_API_KEY env var first."

client = H2OGPTE(address=REMOTE_ADDRESS, api_key=API_KEY)


class QuestionExpecteds(str):
    """
    QuestionExpecteds(
        [
            (
                "Did inflation affect gross profit?", <= question
                [
                    ["Yes, inflation affected gross profit.", "raw material"], # <= must match all
                                        # OR
                    ["inflation did affect gross profit", "raw material"], # <= must match all
                                        # OR
                    ["inflation had an impact", "raw material"], # <= must match all
                ],
            ),
        ]
    ),
    """

    def __init__(self, qe):
        self.qe = qe

    def get(self):
        return self.qe


def get_llms_for_benchmark():
    if os.getenv("INGEST_ONLY"):
        return ["no-op"]
    all_llms = client.get_llm_names()
    if os.getenv("DROP_EXPENSIVE"):
        all_llms = [x for x in all_llms if "claude" not in x]
        all_llms = [x for x in all_llms if "gpt-4" not in x]
    if os.getenv("TEST_ALL"):
        all_llms = [
            x for x in all_llms if "mixtral-8x7b-32768" not in x and "Guard" not in x
        ]
        return all_llms
    return [
        "mistralai/Mixtral-8x7B-Instruct-v0.1",
        # "gemini-1.5-pro-latest",
        # "claude-3-haiku-20240307",
        # "gpt-4-1106-preview",
        # "gpt-4-vision-preview",
    ]  # for CI, test==ship


@pytest.mark.e2e
@pytest.mark.parametrize("llm", get_llms_for_benchmark())
@pytest.mark.parametrize("name, URL, question, expecteds, must_pass", e2e_data)
def test_pdf_questions_e2e(
    name: str,
    URL: str,
    question: str,
    expecteds: str,
    must_pass: bool,
    llm: Union[str, int, None],
):
    import pathlib
    import numpy as np

    cached_file = CachedFile(name, URL)
    question_expecteds = QuestionExpecteds([(question, ast.literal_eval(expecteds))])

    # for test_client_e2e benchmark, need to count all failures, even expected ones
    must_pass |= os.getenv("TEST_ALL") == "1"
    if not must_pass:
        pytest.skip("skipping test since expect failure")

    if os.getenv("SMOKE") and cached_file not in ["Femsa", "Itau", "FastFood"]:
        pytest.skip("skipping for SMOKE since slow")

    file_path: pathlib.Path = cached_file.get()
    collection_name = "Collection for %s" % file_path.name
    time.sleep(np.random.rand() * 2)  # don't want to have race for same collection
    recent_collections = client.list_recent_collections(0, 1000)
    collection_id = None
    chat_session_id = None
    for c in recent_collections:
        if c.name == collection_name:
            collection_id = c.id
            while not client.get_collection(collection_id).document_count:
                time.sleep(1)
                continue
            chat_session_id = client.create_chat_session(collection_id)
            break
    if collection_id is None:
        collection_id = client.create_collection(
            name=collection_name, description="%s" % file_path
        )
        chat_session_id = client.create_chat_session(collection_id)
        with open(file_path, "rb") as f:
            upload_id = client.upload(file_path.name, f)
        job = client.ingest_uploads(collection_id, [upload_id])
        assert job.completed
        assert job.passed > 0
        if job.errors and cached_file in ["AudioLabelGenie"]:
            pytest.xfail(reason=f"OOM for whisper model")
        if job.errors and cached_file in ["DemoDataJon"]:
            # demo data jon.zip has one pure image without any text, so the job has errors
            assert (
                job.failed == 0.0
            ), "overall job passed fine even with internal errors"
            assert (
                job.passed == 1.0
            ), "overall job passed fine even with internal errors"
        else:
            assert not job.errors, job.errors
            assert job.failed == 0.0
            assert job.passed == 1.0
        assert client.get_collection(collection_id).document_count, "ingest failed"
    if os.getenv("INGEST_ONLY"):
        return

    assert client.get_collection(
        collection_id
    ).document_count, "must have ingested document by now"
    with client.connect(chat_session_id) as session:
        count_failed = 0
        msg = ""
        for question, expecteds in question_expecteds.get():
            reply = None
            try:
                reply = session.query(
                    question,
                    timeout=900,
                    llm=llm,
                    rag_config=dict(rag_type="auto"),
                )
            except Exception as e:
                if llm in ["gemini-pro"] and "ValueError: block_reason: SAFETY" in str(
                    e
                ):
                    # only known failure that happens reliably due to overly strong safety feature
                    reply_content = str(e)
                else:
                    raise
            refs = ""
            if reply is not None:
                reply_content = reply.content
                assert len(
                    client.list_chat_message_references(reply.id)
                ), "must have references"
                for ref in client.list_chat_message_references(reply.id):
                    chunks = client.get_chunks(collection_id, [ref.chunk_id])
                    assert len(chunks) == 1
                    selections = json.loads(ref.pages)["selections"]
                    if selections:
                        page_start = selections[0]["page"]
                    else:
                        page_start = 1
                    refs += "[score: %s, page: %d] " % (ref.score, page_start)
                    for c in chunks:
                        refs += c.text + "\n\n"
            error_msg = ""
            for expected in expecteds:
                missings = [
                    e for e in expected if e.lower() not in reply_content.lower()
                ]
                if not missings:
                    # one complete set of expected strings is enough to pass the test
                    error_msg = ""
                    break
                else:
                    error_msg += str(missings)
            if error_msg:
                missing = (
                    "missing: %s, reply: '%s', question: '%s', references: '%s'\n\n"
                    % (error_msg, reply_content, question, refs)
                )
                msg += missing
                count_failed += 1
        if count_failed:
            raise RuntimeError(
                "Failed: %d (out of %d), Errors: %s"
                % (
                    count_failed,
                    len(question_expecteds),
                    msg,
                )
            )


@pytest.mark.xfail(raises=FileNotFoundError, strict=False)
def test_pass_rate_e2e():
    from bs4 import BeautifulSoup

    with open("./test_client.xml", "r") as f:
        bs_data = BeautifulSoup(f.read(), "xml")
    ts = bs_data.find("testsuite")
    hostname = ts["hostname"]
    test_list = ts.contents
    print(test_list)
    llms = client.get_llm_names()
    llms = sorted(
        llms, key=lambda x: len(x), reverse=True
    )  # sort by length, longest one first, to avoid partial matches below

    times = {}
    passes = {}
    fails = {}
    fail_msgs = {}
    fail_questions = {}
    for test in test_list:
        llm = None
        tn = None
        for _llm in llms:
            tn = test.get("name")
            if tn and _llm in tn:
                llm = _llm
                break
        if not tn:
            continue
        dataset = None
        url = None
        for test_row in e2e_data:
            if test_row[0] + "-" in tn:
                dataset = test_row[0]
                url = test_row[1]
        assert dataset, "must find dataset in test"
        if llm not in times:
            times[llm] = 0
        else:
            times[llm] += float(test["time"])
        failure = test.find("failure")
        if failure is None:
            if llm not in passes:
                passes[llm] = 1
            else:
                passes[llm] += 1
        else:
            msg = failure.contents[0]
            qq = None
            try:
                if "question = " in msg:
                    splits = failure.contents[0].split("question =")
                    s = splits[-1]
                    qq = s[: s.find("expecteds =")].strip()
                if 0 <= msg.find("TimeoutError") < msg.find("RuntimeError"):
                    msg = msg[msg.find("message = ") + 10 :]
                    msg = msg[: msg.find(",")]
                    if qq is None:
                        qq = msg
                    msg = "TimeoutError"
                else:
                    msg = msg.split("RuntimeError")[2]
                    msg = msg[msg.find("Errors: ") + 8 : msg.rfind(", references:")]
            except:
                msg = msg[msg.find("raise SessionError") + 19 :]
            msg = f"[{dataset}]({url}) " + msg
            if qq is not None:
                q = qq
            else:
                splits = q = msg.split("question: ")
                if len(splits) > 1:
                    q = splits[-1]
            if q[-1] == ",":
                q = q[:-1]
            if q not in fail_questions:
                fail_questions[q] = 1
            else:
                fail_questions[q] += 1
            if llm not in fails:
                fails[llm] = 1
                fail_msgs[llm] = [msg]
            else:
                fails[llm] += 1
                fail_msgs[llm].append(msg)
    import pandas as pd

    usage_cost_table = client.get_llm_usage_24h_by_llm()
    llm_cost_dict = {u.llm_name: u.llm_cost for u in usage_cost_table}
    perf_table = client.get_llm_performance_by_llm("24 hours")
    llm_vision_map = client.get_llm_and_auto_vision_llm_names()
    perf_frame = pd.DataFrame(
        data={
            "LLM": [p.llm_name for p in perf_table],
            "CALLS": [p.call_count for p in perf_table],
            "INPUT_TOKENS": [p.input_tokens for p in perf_table],
            "OUTPUT_TOKENS": [p.output_tokens for p in perf_table],
            "TOKENS_PER_SECOND": [p.tokens_per_second for p in perf_table],
            "TIME_TO_FIRST_TOKEN": [p.time_to_first_token for p in perf_table],
        }
    )
    cost_frame = pd.DataFrame(
        data={
            "LLM": llms,
            "LLM[VISION]": [llm_vision_map.get(llm) for llm in llms],
            "COST": [llm_cost_dict.get(llm, -1) for llm in llms],
            "PASS": [passes.get(llm, 0) for llm in llms],
            "FAIL": [fails.get(llm, 0) for llm in llms],
            "ACCURACY [%]": [
                passes.get(llm, 0)
                * 100
                / max(1, (passes.get(llm, 0) + fails.get(llm, 0)))
                for llm in llms
            ],
            "TIME": [times.get(llm, -1) for llm in llms],
        }
    )
    results_frame = (
        cost_frame.merge(perf_frame, left_on="LLM", right_on="LLM")
        .sort_values(["PASS", "COST"], ascending=[False, True])
        .reset_index(drop=True)
    )
    results_frame.index = range(1, results_frame.shape[0] + 1)
    results_frame.index.name = "RANK"

    questions_frame = (
        pd.DataFrame(
            data={
                "QUESTION": list(fail_questions.keys()),
                "FAIL": list(fail_questions.values()),
                "FAIL FREQ [%]": [
                    100 * value / len(llms) for value in fail_questions.values()
                ],
            }
        )
        .sort_values(["FAIL"], ascending=[False])
        .reset_index(drop=True)
    )

    skipped = int(ts.get("skipped"))
    if not os.getenv("SMOKE"):
        assert skipped in [
            0,
            22,  # whisper OOM xfail (11 models x 2 tests)
        ], "make sure to run `make test_client_e2e` with TEST_ALL=1"
    with open("results/test_client_e2e.md", "wt") as f:
        try:
            import h2ogpte_core.settings as core_settings
            import h2ogpte_parse.settings as parse_settings
            import h2ogpte_crawl.settings as crawl_settings
            import h2ogpte_chat.settings as chat_settings
            import h2ogpte_vex.settings as vex_settings

            dicts = ""
            for s in [
                core_settings,
                parse_settings,
                crawl_settings,
                chat_settings,
                vex_settings,
            ]:
                dicts += f"### {s.__name__}\n\n`{s.settings.dict()}`\n"
        except ImportError:
            # no modules installed on GH env that only does make setup-pytest
            # that's ok - we don't need above settings, it's clear from git sha.
            dicts = ""

        f.write("-" * 100)
        f.write("\n")
        f.write("# h2oGPTe RAG Benchmarks\n\n")
        f.write("git sha: ")
        f.write(
            subprocess.check_output(["git", "rev-parse", "--short", "HEAD"])
            .decode("ascii")
            .strip()
        )
        f.write("\n\n")
        f.write("Date: " + str(datetime.now()))
        f.write("\n\n")
        f.write("Host: " + hostname)
        f.write("\n\n")
        f.write(f"Total cost: {results_frame['COST'].sum()} USD")
        f.write("\n\n")
        f.write(f"\n## Results:\n")
        f.write(f"```\n")
        f.write(results_frame.to_markdown())
        f.write(f"\n```")
        f.write("\n\n")
        f.write(f"\n## Questions:\n")
        f.write(questions_frame.to_markdown())
        f.write("\n\n")
        f.write(f"\n## Failures:\n")
        for llm in llms:
            if llm in fail_msgs:
                f.write(f"### {llm}\n")
                for ff in sorted(fail_msgs[llm], key=lambda x: x.lower()):
                    f.write(f"   - {ff}\n")
                f.write("\n")
        f.write(f"\n## Accuracy Stats:\n")
        f.write(
            json.dumps(
                dict(
                    zip(
                        results_frame["LLM"].to_list(),
                        results_frame["ACCURACY [%]"].to_list(),
                    )
                ),
                indent=2,
            )
        )
        f.write("\n")
        f.write(f"\n## Settings:\n")
        f.write(dicts)
        f.write("\n")
