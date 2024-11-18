import ast
import copy
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from statistics import mode
from typing import Union, Any, Tuple, List
from bs4 import BeautifulSoup
from agents_test_utils import (
    get_agents_chat_history,
    sanitize,
    get_agent_files,
    get_agents_chat_history_md,
    get_agents_analysis,
    most_common_or_default,
)
import pytest
import html
import pandas as pd
from collections import defaultdict

from conftest import e2e_data, gaia_data, set_num_openblas_threads

set_num_openblas_threads(1)

from h2ogpte import H2OGPTE

assert e2e_data, "Must have Q&A RAG dataset."
assert gaia_data, "Must have GAIA dataset"

try:
    # # For h2ogpte CI
    from mux_py.tests.test_mux import REMOTE_ADDRESS, API_KEY
    from parse.tests.datasets import CachedFile
    from mux_py.tests.test_mux import get_test_name
    from mux_py.tests.agents_test_utils import agent_llms
except ModuleNotFoundError as e:
    # For open-source benchmarks repository
    from datasets import CachedFile

    REMOTE_ADDRESS = os.getenv("H2OGPTE_TEST_ADDRESS", "https://h2ogpte.genai.h2o.ai")
    API_KEY = os.getenv("H2OGPTE_API_KEY")
assert REMOTE_ADDRESS, "Must have h2oGPTe remote server address."
assert API_KEY, "Must set H2OGPTE_API_KEY env var first."

GAIA_RESULTS_DIR = f"{os.getcwd()}/agent_results/gaia"
timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")


def start_faulthandler():
    import faulthandler

    faulthandler.enable()
    if hasattr(faulthandler, "register"):
        import signal

        faulthandler.register(signal.SIGUSR1)


start_faulthandler()

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
    if os.getenv("RUN_GAIA"):
        return agent_llms()
    if os.getenv("INGEST_ONLY"):
        return ["no-op"]
    all_llms = client.get_llm_names()
    if os.getenv("DROP_EXPENSIVE"):
        all_llms = [x for x in all_llms if "claude" not in x]
        all_llms = [x for x in all_llms if "gpt-4" not in x]
    if os.getenv("DROP_MOST_EXPENSIVE_AND_UNNECESSARY", "1") == "1":
        all_llms = [
            x
            for x in all_llms
            if not any(
                y in x
                for y in [
                    "opus",
                    "mistral-large",
                    "mistral-medium",
                    "gemini-1.5-pro-latest",
                    "claude-3-sonnet",
                    "gpt-35-turbo",
                ]
            )
        ]
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


def get_data_for_benchmark():
    if os.getenv("RUN_GAIA"):
        if os.getenv("TEST_ALL"):
            return gaia_data
        # return a question that is usually solved fast
        return gaia_data[0:1]
    return e2e_data


def get_gaia_specific_prompt():
    prompt = """<output_instructions>

* First respond to the user's query however required.
* Then once you have your final answer, you must put your final answer in the <constrained_output> XML tags.
* Your constrained output should be a number OR as few words as possible OR a comma separated list of numbers and/or strings.
* If you are asked for a number, your constrained output shouldn't use comma to write your number and shouldn't use units such as $ or percent sign unless specified otherwise.
* If you are asked for a string, the constrained output shouldn't use articles and should avoid abbreviations (e.g. for cities) and should write the digits in plain text unless specified otherwise.
* If you are asked for a comma separated list, the constrained output should apply the above rules depending of whether the element to be put in the list is a number or a string.  Any list must not include outer brackets and any list must not have quotes around each item.
* Be very careful with instructions regarding rounding and the way to express the output.

</output_instructions>

What follows is the actual user query:

"""
    return prompt


def get_gaia_tool_categories() -> list[str]:
    categories = [
        "web search",
        "search engine",
        "image recognition",
        "pdf handling",
        "calculator",
        "excel",
        "ocr",
        "video recognition",
        "python coding",
        "N/A",
    ]
    return categories


def get_gaia_levels() -> list[str]:
    levels = [
        "0",
        "1",
        "2",
        "3",
    ]
    return levels


# Helper method to compute accuracy scores
def calculate_accuracy(
    passed: pd.Series, tool_results: pd.Series
) -> tuple[Any | None, Any, Any]:
    total = tool_results.sum()
    passed_total = passed[tool_results == 1].sum()
    return (
        round(passed_total / total * 100, 4) if total > 0 else None,
        total,
        passed_total,
    )


def get_gaia_tools_result_frame(
    gaia_tool_categories: dict[str, dict], llms: list[str]
) -> pd.DataFrame | None:
    acc_scores = {}
    categories = get_gaia_tool_categories()

    for llm in llms:
        if llm in gaia_tool_categories["passed"]:
            acc_scores[llm] = {}

            try:
                temp_results = pd.DataFrame(
                    {
                        "PASSED": gaia_tool_categories["passed"][llm],
                        **{
                            category.upper(): gaia_tool_categories[category][llm]
                            for category in categories
                        },
                    }
                )
            except KeyError as e:
                print(f"KeyError: Missing data for {llm} in category: {e}")
                continue
            except Exception as e:
                print(f"Unexpected error for {llm}: {e}")
                continue

            # Calculate accuracy for each category except "PASSED"
            for key in temp_results.columns:
                if key != "PASSED":
                    score, total, passed_total = calculate_accuracy(
                        temp_results["PASSED"], temp_results[key]
                    )
                    acc_scores[llm][key] = f"{score}% - ({passed_total}/{total})"

    rows = [{"LLM": llm, **categories} for llm, categories in acc_scores.items()]
    agent_tool_accuracy_frame = pd.DataFrame(rows)
    if agent_tool_accuracy_frame.empty:
        return None
    else:
        agent_tool_accuracy_frame.set_index("LLM", inplace=True)
        return agent_tool_accuracy_frame


def get_gaia_tool_categories_dict() -> dict[str, dict]:
    categories = get_gaia_tool_categories()
    categories.append("passed")
    return {category: defaultdict(list) for category in categories}


def get_gaia_levels_result_frame(
    gaia_levels: dict[str, dict], llms: list[str]
) -> pd.DataFrame | None:
    acc_scores = {}
    levels = get_gaia_levels()

    for llm in llms:
        if llm in gaia_levels["passed"]:
            acc_scores[llm] = {}

            try:
                temp_results = pd.DataFrame(
                    {
                        "PASSED": gaia_levels["passed"][llm],
                        **{level.lower(): gaia_levels[level][llm] for level in levels},
                    }
                )
            except KeyError as e:
                print(f"KeyError: Missing data for {llm} in category: {e}")
                continue
            except Exception as e:
                print(f"Unexpected error for {llm}: {e}")
                continue

            # Calculate accuracy for each category except "PASSED"
            for key in temp_results.columns:
                if key != "PASSED":
                    score, total, passed_total = calculate_accuracy(
                        temp_results["PASSED"], temp_results[key]
                    )
                    acc_scores[llm][key] = f"{score}% - ({passed_total}/{total})"

    rows = [{"LLM": llm, **levels} for llm, levels in acc_scores.items()]
    agent_tool_accuracy_frame = pd.DataFrame(rows)
    if agent_tool_accuracy_frame.empty:
        return None
    else:
        agent_tool_accuracy_frame.set_index("LLM", inplace=True)
        return agent_tool_accuracy_frame


def get_gaia_levels_dict() -> dict[str, dict]:
    levels = get_gaia_levels()
    levels.append("passed")
    return {level: defaultdict(list) for level in levels}


def read_metadata(llm: str, task_id: str) -> str | None:
    agent_metadata_file = f"{GAIA_RESULTS_DIR}/{llm}/{task_id}/meta_data.json"
    if os.path.isfile(agent_metadata_file):
        try:
            with open(agent_metadata_file, "r") as f:
                metadata = f.read()
                return metadata
        except (json.JSONDecodeError, FileNotFoundError) as e:
            print(f"Error loading metadata from {agent_metadata_file}")
            return
    else:
        print(f"{agent_metadata_file} is not a File. ")


@pytest.mark.e2e
@pytest.mark.clean_up_required
@pytest.mark.parametrize(
    "name, URL, question, expecteds, must_pass, use_agent, tools, tool_categories, level, nsteps",
    get_data_for_benchmark(),
    ids=lambda x: x[0] if isinstance(x, tuple) else x,
)
@pytest.mark.parametrize("llm", get_llms_for_benchmark())
def test_pdf_questions_e2e(
    name: str,
    URL: str,
    question: str,
    expecteds: str,
    must_pass: bool,
    use_agent: bool,
    tools,
    tool_categories: str,
    level: str,
    nsteps: str,
    llm: Union[str, int, None],
):
    import pathlib
    import numpy as np

    file_name = URL.rsplit("/", 1)[1]

    cached_file = CachedFile(name, URL)
    question_expecteds = QuestionExpecteds([(question, ast.literal_eval(expecteds))])

    # for test_client_e2e benchmark, need to count all failures, even expected ones
    must_pass |= os.getenv("TEST_ALL") == "1"
    if not must_pass:
        pytest.skip("skipping test since expect failure")

    if os.getenv("SMOKE") and cached_file not in ["Femsa", "Itau", "FastFood"]:
        pytest.skip("skipping for SMOKE since slow")

    if file_name:
        file_path: pathlib.Path = cached_file.get()
        collection_name = "Collection for %s" % file_path.name
        collection_description = "%s" % file_path
    else:
        collection_name = get_test_name()
        collection_description = "GAIA benchmark run without a file."

    time.sleep(np.random.rand() * 2)  # don't want to have race for same collection
    recent_collections = client.list_recent_collections(0, 1000)
    collection_id = None
    chat_session_id = None
    for c in recent_collections:
        if c.name == collection_name:
            collection_id = c.id
            if not use_agent:
                # Only wait for document counts in RAG case
                while not client.get_collection(collection_id).document_count:
                    time.sleep(1)
                    continue
            chat_session_id = client.create_chat_session(collection_id)
            break
    if collection_id is None:
        collection_id = client.create_collection(
            name=collection_name, description=collection_description
        )
        chat_session_id = client.create_chat_session(collection_id)

        # only upload file to collection in RAG case
        if not use_agent:
            try:
                # random attack for devs with `make test_client_e2e_ingest`
                assert os.getenv("INGEST_ONLY")
                import random

                from h2ogpte_parse.parse import supported_ocr_models
                from h2ogpte_parse.parse import supported_tesseract_languages
                from h2ogpte_core.encode import _get_supported_audio_input_languages

                ocr_model = random.choice(supported_ocr_models)
                audio_input_language = random.choice(
                    list(_get_supported_audio_input_languages().keys())
                )
                keep_tables_as_one_chunk = random.choice([False, True])
                tesseract_lang = random.choice(supported_tesseract_languages)
                gen_doc_summaries = random.choice([False, True])
                gen_doc_questions = random.choice([False, True])
                handwriting_check = random.choice([False, True])
                topic_model = random.choice([True])
                print("doing random attack")
            except:
                print("not doing random attack")
                ocr_model = None
                audio_input_language = "auto"
                keep_tables_as_one_chunk = False
                tesseract_lang = None
                gen_doc_summaries = False
                gen_doc_questions = False
                handwriting_check = False
                topic_model = False

            with open(file_path, "rb") as f:
                upload_id = client.upload(file_path.name, f)
            job = client.ingest_uploads(
                collection_id,
                [upload_id],
                gen_doc_summaries=gen_doc_summaries,
                gen_doc_questions=gen_doc_questions,
                audio_input_language=audio_input_language,
                ocr_model=ocr_model,
                tesseract_lang=tesseract_lang,
                keep_tables_as_one_chunk=keep_tables_as_one_chunk,
                handwriting_check=handwriting_check,
            )
            try:
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
                document_count = client.get_collection(collection_id).document_count
                assert document_count, "ingest failed"
            except AssertionError as assertionException:
                client.delete_collections([collection_id])
                raise assertionException

            if topic_model:
                client.create_topic_model(collection_id)

    if os.getenv("INGEST_ONLY"):
        return
    if not use_agent:
        # only check for documents in RAG case
        assert client.get_collection(
            collection_id
        ).document_count, "must have ingested document by now"
    with client.connect(chat_session_id) as session:
        count_failed = 0
        msg = ""
        for question, expecteds in question_expecteds.get():
            reply = None
            try:
                if use_agent:
                    prompt = get_gaia_specific_prompt()
                    if file_name:
                        prompt += f"<files>\nUse the file(s) at the url to complete your task {URL}\n</files>\n\n"

                    prompt += f"<task>\n{question}\n</task>\n"

                    start_time = time.time()
                    reply = session.query(
                        message=prompt,
                        llm=llm,
                        llm_args={
                            "use_agent": True,
                            "agent_accuracy": "maximum",
                            "client_metadata": str(name + "_" + llm + "_" + question),
                        },
                        rag_config={
                            "rag_type": "llm_only",
                        },
                        timeout=3600,
                    )

                    # Collect and save metadata for report generation
                    agent_response_time = time.time() - start_time
                    agent_chat_history = get_agents_chat_history(
                        client=client, reply=reply
                    )

                    agents_chat_history_txt = ""
                    if agent_chat_history:
                        # Turn chat history into a readable format (for report generation)
                        for entry in agent_chat_history:
                            chat_entry = (
                                f"##### {entry['role']}:\n" f"{entry['content']}\n"
                            )
                            agents_chat_history_txt += chat_entry

                    meta_data = {
                        "task_id": name,
                        "prompt": prompt,
                        "response": reply.content,
                        "expected_answer": expecteds,
                        "chat_history": agents_chat_history_txt,
                        "llm": llm,
                        "agent_response_time": f"{round(agent_response_time, 3)}",
                        "human_tools": tools,
                        "tool_categories": tool_categories,
                        "level": str(level),
                        "nsteps": str(nsteps),
                        "timestamp": timestamp,
                    }

                    results_dir = f"{GAIA_RESULTS_DIR}/{llm}/{name}/"
                    if os.path.exists(results_dir):
                        shutil.rmtree(results_dir)
                    os.makedirs(results_dir)

                    with open(os.path.join(results_dir, "meta_data.json"), "w") as f:
                        f.write(json.dumps(meta_data, indent=4))

                    # Make sure that metadata is saved even if there is no chat history.
                    assert len(agent_chat_history) > 0, "Must have agent chat history."

                    # confirm contents of documents
                    test_name = (
                        name
                        + "_"
                        + llm.replace("/", "_")
                        + "_"
                        + sanitize(question.replace("/", "_"), max=10)
                    )
                    test_name = sanitize(test_name)
                    test_dir = os.path.join(
                        "agent_results",
                        test_name,
                    )

                    shutil.rmtree(test_dir, ignore_errors=True)
                    os.makedirs(test_dir)

                    # check got the file
                    agent_files, agent_values, agent_files_base_names = get_agent_files(
                        client=client, reply=reply
                    )
                    print(agent_values, file=sys.stderr)

                    # check agents talked to each other
                    agents_chat_history = get_agents_chat_history(
                        client=client, reply=reply
                    )
                    agents_chat_history_md = get_agents_chat_history_md(
                        client=client, reply=reply
                    )
                    agents_analysis = get_agents_analysis(client=client, reply=reply)

                    for document in agent_files:
                        for document_id1, document_name1 in document.items():
                            if os.path.exists(os.path.join(test_dir, document_name1)):
                                document_name1 = "alt_" + document_name1
                            client.download_document(
                                test_dir, document_name1, document_id1
                            )

                    with open(os.path.join(test_dir, "response.txt"), "w") as f:
                        f.write(reply.content)

                    with open(os.path.join(test_dir, "chat_history.txt"), "w") as f:
                        f.write(str(agents_chat_history))

                    chat_history_file = os.path.join(test_dir, "chat_history.md")
                    with open(chat_history_file, "w") as f:
                        f.write(str(agents_chat_history_md))
                    os.system(
                        f'pandoc +RTS -M500m -RTS -f markdown {chat_history_file} > {chat_history_file.replace(".md", ".html")}'
                    )

                    with open(os.path.join(test_dir, "agent_analysis.txt"), "w") as f:
                        f.write(str(agents_analysis))

                else:
                    reply = session.query(
                        question,
                        timeout=900,
                        llm=llm,
                        rag_config=dict(
                            rag_type="auto",
                            meta_data_to_include={
                                "text": True,
                                "captions": True,
                                "name": True,
                                "page": True,
                            },
                        ),
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
                assert len(reply_content) > 0, "Must have response"
                # only check for references in RAG case
                if not use_agent:
                    assert len(client.list_chat_message_references(reply.id)), (
                        "must have references: %s" % reply_content
                    )
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
                if use_agent and os.getenv("RUN_GAIA"):
                    assert len(expected) == 1, "must have only one expected for GAIA"
                    from mux_py.tests.gaia_scorer import question_scorer

                    if not question_scorer(reply_content, expected[0]):
                        missings = [expected[0]]
                    else:
                        missings = []
                else:
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


def parse_xml_file(file_path: str) -> BeautifulSoup:
    """
    Helper function to read and parse the XML file.

    Args:
        file_path (str): Path to the XML file.

    Returns:
        BeautifulSoup: Parsed XML data.
    """
    from bs4 import BeautifulSoup

    try:
        with open(file_path, "r") as f:
            bs_data: BeautifulSoup = BeautifulSoup(f.read(), "xml")
        return bs_data
    except FileNotFoundError:
        # Raise the exception to let pytest handle it
        raise


def extract_question(msg: str) -> str | None:
    """Extracts the question from the failure message."""
    if "question = " in msg:
        splits = msg.split("question =")
        question = splits[-1].strip()
        # Get the part before "expecteds ="
        question = question[: question.find("expecteds =")].strip()
        return question
    return None


def parse_failure_message(
    failure: BeautifulSoup, dataset: str, url: str
) -> tuple[list | None | Any, str | Any]:
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
    return q, msg


def escape_html_tags_and_render_as_code(text: str) -> str:
    escaped_text = html.escape(text)
    code_text_html = "<pre><code>\n" f"```\n{escaped_text}\n```\n" "</code></pre>\n"
    return code_text_html


def create_failure_report_from_metadata(
    f, metadata_str: str = None, metadata_dict: str = None, llm: str = ""
):
    if metadata_str:
        metadata_dict = json.loads(metadata_str)
    assert llm == metadata_dict["llm"]

    f.write(f"**Task ID**: {metadata_dict['task_id']}\n\n")
    f.write(f"**Timestamp**: {metadata_dict['timestamp']}\n\n")
    f.write(f"**Expected Answer**: {metadata_dict['expected_answer']}\n\n")
    f.write(f"**Agent Response**: {metadata_dict['response']}\n\n")
    f.write(f"**Agent Response Time**: {metadata_dict['agent_response_time']}\n\n")
    f.write(f"**Human Tools**: {metadata_dict['human_tools']}\n\n")
    f.write(f"**Prompt**\n\n")
    prompt = escape_html_tags_and_render_as_code(metadata_dict["prompt"])
    f.write(prompt)
    f.write(f"\n\n")
    chat_history = metadata_dict["chat_history"]
    escaped_chat_history = html.escape(chat_history)
    collapsed_chat_history = (
        "<details>\n"
        "\t<summary>Chat History</summary>\n\n"
        "<pre><code>\n"
        f"```\n{escaped_chat_history}\n```\n"
        "</code></pre>\n"
        "</details>\n"
    )
    f.write(collapsed_chat_history)
    f.write("\n\n")
    f.write("***")


@pytest.mark.xfail(raises=FileNotFoundError, strict=False)
def test_pass_rate_e2e():
    with open("./test_client.xml", "r") as f:
        bs_data = BeautifulSoup(f.read(), "xml")

    test_suite = bs_data.find("testsuite")
    hostname = test_suite["hostname"]
    # avoid to get all direct children of that tag
    # (other tags, text nodes, and even whitespaces)
    test_list = test_suite.find_all("testcase")
    print(test_list)

    if os.getenv("RUN_GAIA"):
        # cd ~/Downloads/all_main_gaia
        # Run bash ~/h2ogpte/scripts/gaia_artifacts_download.sh
        # Run bash ~/h2ogpte/scripts/gaia_artifacts_download_1.sh 11861110789 (for new gaia run) for specific new runs if just few new runs needed to avoid redoing entire download)
        # Run test_merge_many_runs
        # then set do_merged = True
        # Then run this test
        do_merged = True
        if do_merged:
            llm_sonnet = "11877726721"
            llms = [
                "11568839878",
                "11588463470",
                "11607498444",
                "11625669152",
                "11640765003",
                "11649329718",
                "11659472213",
                "11679119205",
                "11698710518",
                "11737590601",
                "11754230597",
                "11763065795",
                "11784555244",
                "11861110789",
                "11867920275",
                "11869277441",
                "11877726721",
            ]
        else:
            llms = get_llms_for_benchmark()
            llm_sonnet = "claude-3-5-sonnet-20240620"
    else:
        llms = client.get_llm_names()

    llms = sorted(
        llms, key=lambda x: len(x), reverse=True
    )  # sort by length, longest one first, to avoid partial matches below

    benchmark_data = get_data_for_benchmark()

    times = defaultdict(float)
    passes = defaultdict(int)
    fails = defaultdict(int)
    agent_response_times = defaultdict(list)
    gaia_tool_categories = get_gaia_tool_categories_dict()
    gaia_levels = get_gaia_levels_dict()
    fail_msgs = defaultdict(list)
    fail_questions = defaultdict(int)

    metadata_str_dict = {}
    metadata_str_dict_by_name = {}
    for test in test_list:
        test_name = test.get("name")
        if not test_name:
            print(f"Warning: Test case without 'name' attribute: {test}")
            continue  # Skip if no test name found

        llm = None
        for _llm in llms:
            if _llm in test_name:
                llm = _llm
                break

        dataset = None
        url = None
        for test_row in benchmark_data:
            if test_row[0] + "-" in test_name:
                dataset = test_row[0]
                url = test_row[1]
        assert dataset, "must find dataset in test"

        times[llm] += float(test["time"])

        failure = test.find("failure")

        if os.getenv("RUN_GAIA"):
            metadata_str = read_metadata(llm=llm, task_id=dataset)
            if metadata_str is not None:
                metadata_dict = json.loads(metadata_str)
                metadata_str_dict[(test_name, llm)] = dict(
                    metadata_str=metadata_str,
                    metadata_dict=metadata_dict,
                    llm=llm,
                    test_name=test_name,
                    failure=failure,
                    times=times[llm],
                    dataset=dataset,
                    url=url,
                )
                metadata_str_dict_by_name[(dataset, llm)] = metadata_str_dict[
                    (test_name, llm)
                ]
                agent_response_times[llm].append(
                    float(metadata_dict.get("agent_response_time", 0))
                )
        if failure is None:
            passes[llm] += 1
        else:
            q, msg = parse_failure_message(failure=failure, dataset=dataset, url=url)
            if os.getenv("RUN_GAIA"):
                q = re.sub(r"([*_\[\]`])", r"\\\1", q)
                q = html.escape(q)
            fail_questions[q] += 1
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

    cost_frame_data = {
        "LLM": llms,
        "LLM[VISION]": [llm_vision_map.get(llm) for llm in llms],
        "COST": [llm_cost_dict.get(llm, -1) for llm in llms],
        "PASS": [passes.get(llm, 0) for llm in llms],
        "FAIL": [fails.get(llm, 0) for llm in llms],
        "ACCURACY [%]": [
            passes.get(llm, 0) * 100 / max(1, (passes.get(llm, 0) + fails.get(llm, 0)))
            for llm in llms
        ],
        "TIME": [times.get(llm, -1) for llm in llms],
    }
    if os.getenv("RUN_GAIA"):
        cost_frame_data["MEAN RESPONSE TIME"] = [
            sum(agent_response_times.get(llm, [0]))
            / max(1, len(agent_response_times.get(llm, [0])))
            for llm in llms
        ]
    cost_frame = pd.DataFrame(data=cost_frame_data)

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

    gaia_tool_performance_frame = None
    gaia_level_performance_frame = None

    # add PASS_ANY that indicates pass if any llm passed
    if os.getenv("RUN_GAIA"):
        from mux_py.tests.gaia_scorer import question_scorer, normalize_answer

        # PASS_ANY
        for (name, llm), metadata_str_dict1 in metadata_str_dict_by_name.copy().items():
            if (name, "PASS_ANY") not in metadata_str_dict_by_name:
                metadata_str_dict_by_name[(name, "PASS_ANY")] = copy.deepcopy(
                    metadata_str_dict1
                )
                metadata_str_dict_by_name[(name, "PASS_ANY")]["failure"] = list()
            failure = metadata_str_dict_by_name[(name, llm)]["failure"]
            metadata_str_dict_by_name[(name, "PASS_ANY")]["failure"].append(failure)
        for (name, llm), metadata_str_dict1 in metadata_str_dict_by_name.copy().items():
            if llm == "PASS_ANY":
                failure = metadata_str_dict_by_name[(name, "PASS_ANY")]["failure"]
                metadata_str_dict_by_name[(name, "PASS_ANY")]["failure"] = (
                    None if any(x is None for x in failure) else "failure"
                )
        llms.append("PASS_ANY")

        # MODE_LLM
        for (name, llm), metadata_str_dict1 in metadata_str_dict_by_name.copy().items():
            print(f"\nname:{name}", file=sys.stdout)
            orig_response = metadata_str_dict_by_name[(name, llm)]["metadata_dict"][
                "response"
            ]
            if "# filename" in orig_response:
                orig_response = ""
            bad_str = "\n![image]"
            if isinstance(orig_response, str) and bad_str in orig_response:
                index_bad = orig_response.index(bad_str)
                print("cleaned %s" % orig_response)
                orig_response = orig_response[:index_bad]
            if orig_response == "infinity":
                orig_response = ""

            if (name, "MODE_LLM") not in metadata_str_dict_by_name:
                metadata_str_dict_by_name[(name, "MODE_LLM")] = copy.deepcopy(
                    metadata_str_dict1
                )
                metadata_str_dict_by_name[(name, "MODE_LLM")]["metadata_dict"][
                    "orig_response"
                ] = orig_response
                metadata_str_dict_by_name[(name, "MODE_LLM")]["metadata_dict"][
                    "response"
                ] = list()
            if llm != "PASS_ANY":
                response = orig_response
                if len(response) > 100:
                    print(f"BADNAME {name}")
                print(
                    f"\nresponse: {response}\nnormalized_response: {normalize_answer(response)}",
                    file=sys.stdout,
                )
                if response:
                    response = normalize_answer(response)
                    if isinstance(response, list):
                        response = tuple(response)
                    metadata_str_dict_by_name[(name, "MODE_LLM")]["metadata_dict"][
                        "response"
                    ].append(response)
        for (name, llm), metadata_str_dict1 in metadata_str_dict_by_name.copy().items():
            if llm == "MODE_LLM":
                response = metadata_str_dict_by_name[(name, "MODE_LLM")][
                    "metadata_dict"
                ]["response"]
                orig_response = metadata_str_dict_by_name[(name, "MODE_LLM")][
                    "metadata_dict"
                ]["orig_response"]
                response_sonnet = metadata_str_dict_by_name[(name, llm_sonnet)][
                    "metadata_dict"
                ]["response"]
                response_list = response  # DEBUG
                if response:
                    print(f"\nresponse_list: {response_list}", file=sys.stdout)
                    if len(set(response)) == len(response):
                        # if nobody agrees, go with smartest model
                        response = response_sonnet
                    else:
                        response = most_common_or_default(response, response_sonnet)
                        if isinstance(response, tuple):
                            response = ", ".join(map(str, response))
                        else:
                            response = str(response)
                else:
                    response = ""
                expected = metadata_str_dict_by_name[(name, "MODE_LLM")][
                    "metadata_dict"
                ]["expected_answer"]
                good_answer0 = question_scorer(orig_response, expected[0][0])
                good_answer = question_scorer(response, expected[0][0])

                # DEBUG:
                good_sonnet = question_scorer(response_sonnet, expected[0][0])
                sonnet_good_mode_bad = not good_answer and good_sonnet
                if sonnet_good_mode_bad:
                    print(
                        f"sonnet_good_mode_bad: {name} {response_list}", file=sys.stdout
                    )

                checking_one = False
                if checking_one:
                    if good_answer0 != good_answer:
                        print(name)

                metadata_str_dict_by_name[(name, "MODE_LLM")]["failure"] = (
                    None if good_answer else "failure"
                )
        llms.append("MODE_LLM")

        # use test if test set
        df = pd.read_csv("parse/tests/gaia_validation_df.csv")

        for (name, llm), metadata_str_dict1 in metadata_str_dict_by_name.items():
            metadata_dict = metadata_str_dict1["metadata_dict"]
            failure = metadata_str_dict1["failure"]
            level_meta = metadata_dict.get("level")

            df2 = df[df["name"] == name]
            assert df2.shape[0] == 1
            level = str(df2["level"].values[0])
            if level_meta:
                assert level == level_meta
            else:
                level_meta = str(level)

            for key in gaia_tool_categories:
                if key != "passed":
                    gaia_tool_categories[key][llm].append(
                        1 if key in metadata_dict["tool_categories"] else 0
                    )
            gaia_tool_categories["passed"][llm].append(1 if failure is None else 0)

            for key in gaia_levels:
                if key != "passed":
                    if key == "0":
                        gaia_levels[key][llm].append(1)
                    else:
                        gaia_levels[key][llm].append(
                            1 if str(key) == str(level_meta) else 0
                        )
            gaia_levels["passed"][llm].append(1 if failure is None else 0)

        if len(gaia_tool_categories) > 0:
            gaia_tool_performance_frame = get_gaia_tools_result_frame(
                gaia_tool_categories=gaia_tool_categories, llms=llms
            )
        if len(gaia_levels) > 0:
            gaia_level_performance_frame = get_gaia_levels_result_frame(
                gaia_levels=gaia_levels, llms=llms
            )

    skipped = int(test_suite.get("skipped"))
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
        if os.getenv("RUN_GAIA"):
            f.write("# h2oGPTe GAIA Benchmarks\n\n")
        else:
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
        if os.getenv("RUN_GAIA"):
            if gaia_tool_performance_frame is not None:
                f.write(f"\n**Accuracy Scores per Tool Category [%]**\n")
                f.write(gaia_tool_performance_frame.to_markdown())
                f.write("\n\n")
            if gaia_level_performance_frame is not None:
                f.write(f"\n**Accuracy Scores per Level [%]**\n")
                f.write(gaia_level_performance_frame.to_markdown())
                f.write("\n\n")
        f.write(f"\n## Questions:\n")
        f.write(questions_frame.to_markdown())
        f.write("\n\n")
        f.write(f"\n## Failures:\n")
        for llm in llms:
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

    if os.getenv("RUN_GAIA"):
        sorted_metadata = group_and_sort_metadata(metadata_str_dict)

        with open("results/test_client_e2e_gaia.md", "wt") as f:
            for llm, test_cases in sorted_metadata:
                f.write(f"### {llm}\n")
                for test_name, d in test_cases:
                    f.write(f"### {test_name}\n")
                    metadata_dict = d["metadata_dict"]
                    try:
                        create_failure_report_from_metadata(
                            f, metadata_str=None, metadata_dict=metadata_dict, llm=llm
                        )
                    except Exception as e:
                        print(
                            f"Error creating failure report for {llm} - {test_name}: {e}"
                        )


def group_and_sort_metadata(metadata_str_dict):
    # Group by LLM
    grouped_data = defaultdict(list)
    for (test_name, llm), d in metadata_str_dict.items():
        grouped_data[llm].append((test_name, d))

    # Sort LLMs and test cases
    sorted_data = sorted(grouped_data.items())
    return [
        (llm, sorted(test_cases, key=lambda x: x[0])) for llm, test_cases in sorted_data
    ]


def test_merge_many_runs():
    import os
    import tarfile
    import zipfile
    import shutil
    import json
    from pathlib import Path
    from bs4 import BeautifulSoup

    # Input parameters
    target_llm_name = "claude-3-5-sonnet-20240620"  # Only include data for this LLM
    artifacts_dir = "/home/jon/Downloads/all_main_gaia/artifacts"
    base_output_dir = os.path.join("./", "agent_results", "gaia")
    merged_test_client_path = os.path.join("./", "test_client.xml")

    # Ensure the output directory exists
    temp_output_dir = os.path.join("./", "temp")
    os.makedirs(base_output_dir, exist_ok=True)
    os.makedirs(os.path.dirname(merged_test_client_path), exist_ok=True)

    # Initialize containers for merged test cases and attributes
    merged_test_cases = []
    aggregated_attributes = {"skipped": 0, "errors": 0, "failures": 0, "tests": 0}

    # Function to extract a specific file from a ZIP archive
    def extract_file_from_zip(zip_path, target_file, extract_to):
        with zipfile.ZipFile(zip_path, "r") as zip_ref:
            for file in zip_ref.namelist():
                if file.endswith(target_file):
                    zip_ref.extract(file, extract_to)
                    return os.path.join(extract_to, file)
        raise FileNotFoundError(f"{target_file} not found in {zip_path}")

    # Function to extract a specific folder from a tar.gz archive
    def extract_folder_from_tar_gz(tar_path, target_folder, extract_to):
        with tarfile.open(tar_path, "r:gz") as tar_ref:
            for member in tar_ref.getmembers():
                if member.name.startswith(target_folder):
                    tar_ref.extract(member, extract_to)
            return os.path.join(extract_to, target_folder)

    # Recursively copy meta_data.json files to the new structure
    def copy_meta_data_files(base_path, llm_name, llm_label, output_base):
        llm_path = os.path.join(base_path, llm_name)
        if os.path.exists(llm_path):
            for root, _, files in os.walk(llm_path):
                if "meta_data.json" in files:
                    task_id = Path(root).name
                    output_dir = os.path.join(output_base, llm_label, task_id)
                    os.makedirs(output_dir, exist_ok=True)
                    shutil.copy(
                        os.path.join(root, "meta_data.json"),
                        os.path.join(output_dir, "meta_data.json"),
                    )

    # Iterate through all RUN_ID directories
    run_dirs = [
        os.path.join(artifacts_dir, run_id)
        for run_id in os.listdir(artifacts_dir)
        if os.path.isdir(os.path.join(artifacts_dir, run_id))
    ]

    llm_labels = []
    for run_dir in run_dirs:
        run_id = os.path.basename(run_dir)  # Use run_id as the label
        llm_label = run_id  # Rename llm_label to run_id
        llm_labels.append(llm_label)
        print(f"Processing artifacts in {run_dir} as {llm_label}...")

        # Create a temporary directory to extract files
        temp_dir = os.path.join(temp_output_dir, f"temp_{llm_label}")
        os.makedirs(temp_dir, exist_ok=True)

        # Extract gaia folder
        try:
            zip_path = os.path.join(run_dir, "agent_results tar gz.zip")
            tar_gz_path = extract_file_from_zip(
                zip_path, "agent_results.tar.gz", temp_dir
            )
            gaia_folder = extract_folder_from_tar_gz(
                tar_gz_path, "agent_results/gaia", temp_dir
            )
        except (FileNotFoundError, zipfile.BadZipFile) as e:
            print(f"Error: {e}")
            continue

        # Copy meta_data.json files to the new structure
        copy_meta_data_files(gaia_folder, target_llm_name, llm_label, base_output_dir)

        # Extract test_client.xml
        try:
            test_client_path = extract_file_from_zip(
                os.path.join(run_dir, "test_client_xml.zip"),
                "test_client.xml",
                temp_dir,
            )
        except FileNotFoundError as e:
            print(f"Error: {e}")
            continue

        # Read and merge test_client.xml, filtering only the target LLM
        with open(test_client_path, "r") as xml_file:
            bs_data = BeautifulSoup(xml_file.read(), "xml")
            test_suite = bs_data.find("testsuite")
            if not test_suite:
                print(f"No testsuite found in {test_client_path}")
                continue

            # Aggregate attributes
            for attr in aggregated_attributes.keys():
                aggregated_attributes[attr] += int(test_suite.get(attr, 0))

            test_suite["hostname"] = llm_label  # Update hostname to run_id
            for test_case in test_suite.find_all("testcase"):
                if target_llm_name in test_case.get(
                    "name", ""
                ):  # Include only target LLM test cases
                    test_case["name"] = test_case["name"].replace(
                        target_llm_name, llm_label
                    )  # Rename LLM in test case name
                    merged_test_cases.append(test_case)

        # Clean up temporary directory
        shutil.rmtree(temp_dir)

    print("llm_labels", llm_labels)

    # Write merged test_client.xml
    soup = BeautifulSoup(features="xml")
    test_suite_tag = soup.new_tag(
        "testsuite",
        hostname="merged",
        skipped=str(aggregated_attributes["skipped"]),
        errors=str(aggregated_attributes["errors"]),
        failures=str(aggregated_attributes["failures"]),
        tests=str(aggregated_attributes["tests"]),
    )
    for test_case in merged_test_cases:
        test_suite_tag.append(test_case)

    soup.append(test_suite_tag)
    with open(merged_test_client_path, "w") as xml_file:
        xml_file.write(soup.prettify())
    print(f"Merged test_client.xml saved at {merged_test_client_path}")
