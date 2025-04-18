import ast
import copy
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime
from typing import Union, Any
from bs4 import BeautifulSoup
from agents_test_utils import (
    get_agents_chat_history,
    sanitize,
    get_agent_files,
    get_agents_chat_history_md,
    get_agents_analysis,
    most_common_or_default,
    convert_markdown_to_html,
)
import pytest
import html
import pandas as pd

from conftest import (
    e2e_data,
    gaia_data,
    set_num_openblas_threads,
    reasoning_models_to_keep,
)

set_num_openblas_threads(1)

from h2ogpte import H2OGPTE

assert e2e_data, "Must have Q&A RAG dataset."
assert gaia_data, "Must have GAIA dataset"

from mux_py.tests.agents_test_utils import agent_llms

if os.getenv("NO_SERVER", "0") == "0":
    try:
        from mux_py.tests.test_mux import get_test_name
        from parse.tests.datasets_cached import CachedFile

        # # For h2ogpte CI
        from mux_py.tests.test_mux import REMOTE_ADDRESS, API_KEY
    except ModuleNotFoundError as e:
        # For open-source benchmarks repository
        from datasets_cached import CachedFile

        REMOTE_ADDRESS = os.getenv(
            "H2OGPTE_TEST_ADDRESS", "https://h2ogpte.genai.h2o.ai"
        )
        API_KEY = os.getenv("H2OGPTE_API_KEY", "sk-user_1@example.com")
    assert REMOTE_ADDRESS, "Must have h2oGPTe remote server address."
    assert API_KEY, "Must set H2OGPTE_API_KEY env var first."
    H2OGPTE.TIMEOUT = 6000.0
    client = H2OGPTE(address=REMOTE_ADDRESS, api_key=API_KEY)
    client.TIMEOUT = 6000.0
else:
    client = None

GAIA_RESULTS_DIR = f"{os.getcwd()}/agent_results/gaia"
timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")


def start_faulthandler():
    import faulthandler

    faulthandler.enable()
    if hasattr(faulthandler, "register"):
        import signal

        faulthandler.register(signal.SIGUSR1)


start_faulthandler()


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
                    "gpt-4.5-preview",  # $90 per run
                ]
            )
        ]
    if os.getenv("DROP_REASONING", "1") == "1":
        all_llms = [
            x
            for x in all_llms
            if x not in client.get_reasoning_capable_llm_names()
            or x in reasoning_models_to_keep()
        ]
    if os.getenv("TEST_ALL"):
        all_llms = [
            x for x in all_llms if "mixtral-8x7b-32768" not in x and "Guard" not in x
        ]
        return all_llms
    return [
        "mistralai/Mixtral-8x7B-Instruct-v0.1",
        # "h2oai/h2ovl-mississippi-2b",
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
* Your constrained output should be a number OR a string with as few words as possible OR a comma separated list of numbers and/or strings.
* If you are asked for a number (e.g. what number ...), then your constrained output MUST only contain numerical digits without words, commas, units, dollar sign ($), or a percent sign (%) UNLESS specifically part of the user's actual query.
  - E.g. if a dollar amount, do not respond with "89706.00 USD" just respond with "89706.00"
* If you are asked for a string, the constrained output shouldn't use articles (e.g. a, the) and must avoid abbreviations (e.g. for cities, states, etc.) and must write the digits in plain text.
  - However, if articles, abbreviations, and plain text numbers are specifically in user's actual query, then you must include them in your response.
  - However, if doing translation, anagrams, or decoding messages, preserve all words, letters, and punctuation (including ending periods).
  - If the string comes from a document given, use the same exact spelling (e.g. document refers to string Hotels and that is the answer, then don't shorten to Hotel, use what document uses)
  - If the string comes from a website, use the same exact spelling (e.g. website text or image refers to "citations", then don't convert to "citation count", just leave as original).
  - For any string, avoid use of commas because that would be interpreted as a list.  E.g., for an answer that is to give month and year, avoid comma between month and year (e.g. give "January 2022" but not "January, 2022").
* If you are asked for a comma separated list, the constrained output should apply the above rules depending of whether the element to be put in the list is a number or a string.  Any list must not include outer brackets and any list must not have quotes around each item.  Lists should not be numbered or contain new lines.
  - If the list would include strings from document, website, image, or transcription, then use the exact spelling and exact relevant phrasing from the source (e.g. if ingredients are given, you must not remove key relevant details like 'freshly squeezed' or 'ripe' or 'pure' etc. unless those descriptions do not exist or you are explicitly asked to shorten).
* Be very careful with instructions regarding rounding and the way to express the output.
* Be very careful to distinguish between user asking in same query to "find" and to "tell", where if both appear you should put the answer from the "tell" part in your constrained output.
* Be very careful with scale of numerical values, e.g. if the user asks "how many thousand hours" and the number of hours is 17000 then the answer should be "17" not "17000".
* Be very careful with quantities like percentages, because percentage is unitless and so both terms must be compatible units or have no units, so you must carefully identify type (unit) of quantity for each term in the percentage (e.g. dollar per dollar is ok, but count per dollar as a percentage would be a major error).
* If you have low confidence and were unable to give a good response inside the constrained_output XML tags, then fill the <constrained_output> XML tags with no characters (e.g. don't put unknown or unavailable etc., and don't put 0 just because you failed to find relevant material).
* You must *never* assume that the actual user query is in error or they may be misremembering details.  You must always assume that the user query is correct and that you must find the correct answer based on the user query.

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

                    # choose tools most relevant for GAIA
                    if os.getenv("RUN_GAIA"):
                        agent_tools = [
                            "aider_code_generation.py",
                            "ask_question_about_image.py",
                            "audio_video_transcription.py",
                            "convert_document_to_text.py",
                            "download_web_video.py",
                            "screenshot_webpage.py",
                            "unified_search.py",
                            "google_search.py",
                            "bing_search.py",
                            "browser_agent.py",
                            "scholar_papers_query.py",
                            "query_to_web_image.py",
                            "ask_question_about_documents.py",
                            "wikipedia_article.py",
                            "wayback_machine_search.py",
                            "advanced_reasoning.py",
                            "shell",
                            "python",
                            "internet",
                            "intranet",
                        ]
                    else:
                        agent_tools = "auto"

                    reply = session.query(
                        message=prompt,
                        system_prompt="",
                        llm=llm,
                        llm_args={
                            "use_agent": True,
                            "agent_accuracy": "maximum",
                            "client_metadata": str(name + "_" + llm + "_" + question),
                            "max_time": 3600,
                            "agent_total_timeout": 3000,
                            "agent_tools": agent_tools,
                            "temperature": 0.6
                            if "DeepSeek-R1" in llm and os.getenv("RUN_GAIA")
                            else 0.0,
                            "agent_planning_forced_mode": True,
                            # "agent_main_model": "deepseek-ai/DeepSeek-R1-shadeform",
                            "agent_max_confidence_level": 2,
                            "agent_main_reasoning_effort": 10000,
                            "agent_advanced_reasoning_effort": 20000,
                            "autogen_code_restrictions_level": 0,
                        },
                        rag_config={
                            "rag_type": "llm_only",
                        },
                        timeout=4000,
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
                    (
                        agent_files,
                        agent_files_old,
                        agent_values,
                        agent_files_base_names,
                        agent_files_pdf,
                        agent_files_pdf_old,
                    ) = get_agent_files(client=client, reply=reply)
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
                                document_name1 = str(uuid.uuid4()) + document_name1
                            try:
                                client.download_document(
                                    test_dir, document_name1, document_id1
                                )
                            except Exception as e:
                                fail_msg = f"Failed to download document {test_dir} {document_id1} {document_name1}: {e}"
                                print(fail_msg)
                                with open(
                                    os.path.join(test_dir, "fails.txt"), "a"
                                ) as f:
                                    f.write(fail_msg)

                    with open(os.path.join(test_dir, "response.txt"), "w") as f:
                        f.write(reply.content)

                    with open(os.path.join(test_dir, "chat_history.txt"), "w") as f:
                        f.write(str(agents_chat_history))

                    chat_history_file = os.path.join(test_dir, "chat_history.md")
                    with open(chat_history_file, "w") as f:
                        f.write(str(agents_chat_history_md))
                    convert_markdown_to_html(chat_history_file)

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
                if not use_agent:
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
                assert all(
                    isinstance(e, str) for e in expected
                ), "expected values must be strings"
                if use_agent and os.getenv("RUN_GAIA"):
                    do_validation = os.getenv("DO_VALIDATION", "1") == "1"
                    if do_validation:
                        assert (
                            len(expected) == 1
                        ), "must have only one expected for GAIA"
                        from mux_py.tests.gaia_scorer import question_scorer

                        if not question_scorer(reply_content, expected[0]):
                            missings = [expected[0]]
                        else:
                            missings = []
                    else:
                        # test set, nothing to do
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
    if os.getenv("DO_MERGED", "0") == "0":
        assert llm == metadata_dict["llm"]
    else:
        metadata_dict["llm"] = llm

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


from collections import defaultdict
from typing import List, Dict, Tuple, Any, Optional
import pandas as pd
from collections import Counter


def cleanup_bad_constrained_output(text):
    """
    Clean up badly parsed constrained output where the closing tag is missing.
    If the text contains an opening <constrained_output> tag without a matching
    closing tag, extract everything after the opening tag.

    Args:
        text (str): The text to clean up

    Returns:
        str: The cleaned output or None if no issues detected
    """
    # Check if we have an opening tag without a closing tag
    if "<constrained_output>" in text and "</constrained_output>" not in text:
        # Extract everything after the opening tag
        start_idx = text.find("<constrained_output>") + len("<constrained_output>")
        extracted_content = text[start_idx:].strip()
        return extracted_content

    # If it's a well-formed tag or no tag is present, return None
    # to indicate no cleanup was needed
    return None


def extract_constrained_output(text):
    """
    Extract the constrained output from text, handling both well-formed
    and badly parsed cases.

    Args:
        text (str): The text containing constrained output

    Returns:
        str: The extracted constrained output or None if not found
    """
    # First try the regular extraction for well-formed tags
    if "<constrained_output>" in text and "</constrained_output>" in text:
        pattern = r"<constrained_output>(.*?)</constrained_output>"
        matches = re.findall(pattern, text, re.DOTALL)
        if matches:
            return matches[0].strip()

    # If regular extraction fails or tags are malformed, try cleanup
    cleanup_result = cleanup_bad_constrained_output(text)
    if cleanup_result:
        return cleanup_result

    # If nothing found
    return None


def normalize_response(response: str) -> str:
    """Normalize a response string following existing normalization logic."""
    response_test = extract_constrained_output(response)
    if response_test:
        response = response_test

    if response == "infinity" or response == float("inf"):
        return ""
    if "i apologize" in str(response).lower():
        return ""
    if "<turn_title>" in str(response).lower():
        return ""
    if "fullerror:" in str(response).lower():
        return ""
    if "# filename:" in str(response).lower():
        return ""
    if "**Error:**" in str(response):
        return ""
    if "**Full Error:**" in str(response):
        return ""
    if "**Partial Error:**" in str(response):
        return ""
    if "# filename" in response:
        return ""
    if "Thank you for the reminder".lower() in response.lower():
        return ""

    bad_str = "\n![image]"
    if isinstance(response, str) and bad_str in response:
        index_bad = response.index(bad_str)
        response = response[:index_bad]

    return response


def get_response_mode(responses: List[str]) -> Optional[str]:
    """Get the most common valid response from a list of responses."""
    # Filter out empty responses
    valid_responses = [r for r in responses if r]
    if not valid_responses:
        return None

    # Get mode of responses
    response_counts = Counter(valid_responses)
    mode = response_counts.most_common(1)[0][0]
    return mode


def compute_llm_mode_agreement(
    metadata_dict: Dict[str, Any]
) -> Dict[str, Dict[str, Any]]:
    """
    Compute agreement with mode for each LLM across all tasks.

    Args:
        metadata_dict: Dictionary mapping (task_name, llm) to metadata

    Returns:
        Dictionary mapping LLM names to their agreement statistics
    """
    # Group responses by task and collect all tasks and LLMs
    task_responses = defaultdict(dict)
    llm_set = set()
    task_set = set()

    # First pass: collect all responses, tasks, and LLMs
    for (task_name, llm), data in metadata_dict.items():
        if llm in ["MODE_LLM", "PASS_ANY"]:
            continue

        task_set.add(task_name)
        llm_set.add(llm)

        response = normalize_response(data["metadata_dict"]["response"])
        task_responses[task_name][llm] = response

    # Calculate agreement scores
    llm_agreements = {llm: {"matches": 0, "total": len(task_set)} for llm in llm_set}

    # Second pass: compare each LLM's response to the mode
    for task_name in task_set:
        # Get all responses for this task
        task_llm_responses = task_responses[task_name]
        valid_responses = [r for r in task_llm_responses.values() if r]

        if not valid_responses:
            continue

        mode_response = get_response_mode(valid_responses)
        if not mode_response:
            continue

        # Check agreement for each LLM
        for llm in llm_set:
            # If LLM has no response for this task, it's counted as a disagreement
            # (matches count stays the same, total is already set)
            if llm in task_llm_responses and task_llm_responses[llm] == mode_response:
                llm_agreements[llm]["matches"] += 1

    # Calculate agreement percentages
    llm_scores = {}
    for llm, stats in llm_agreements.items():
        if stats["total"] > 0:
            agreement_rate = (stats["matches"] / stats["total"]) * 100
            llm_scores[llm] = {
                "agreement_rate": agreement_rate,
                "matches": stats["matches"],
                "total": stats["total"],
            }

    return llm_scores


def select_reference_llm(metadata_dict: Dict[str, Any]) -> str:
    """
    Select the best reference LLM based on mode agreement across all tasks.

    Args:
        metadata_dict: Dictionary mapping (task_name, llm) to metadata

    Returns:
        Name of the LLM with highest mode agreement
    """
    llm_scores = compute_llm_mode_agreement(metadata_dict)

    # Sort LLMs by agreement rate and then by total number of tasks
    sorted_llms = sorted(
        llm_scores.items(),
        key=lambda x: (x[1]["agreement_rate"], x[1]["total"]),
        reverse=True,
    )

    if not sorted_llms:
        raise ValueError("No valid LLMs found for reference selection")

    best_llm = sorted_llms[0][0]
    return best_llm


def get_reference_llm(metadata_str_dict: Dict[Tuple[str, str], Dict[str, Any]]) -> str:
    """
    Wrapper function to get reference LLM from metadata dictionary.

    Args:
        metadata_str_dict: Dictionary from test containing metadata for all LLMs and tasks

    Returns:
        Name of the best reference LLM
    """
    reference_llm = select_reference_llm(metadata_str_dict)
    return reference_llm


no_answer_phrases = [
    "Unknown",
    "Inconclusive",
    "Cannot be determined",
    "I don't know",
    "Not sure",
    "No information available",
    "Unable to provide an answer",
    "Insufficient data",
    "Unclear",
    "No definitive answer",
    "Lack of evidence",
    "Not applicable",
    "No relevant information found",
    "Indeterminate",
    "Cannot confirm",
    "Outside my scope of knowledge",
    "Ambiguous",
    "No conclusion can be drawn",
    "The answer is uncertain",
    "No answer provided",
    "Unable to determine",
    "NA",
    "Not available",
    "Unable to access",
    "Unable to determine source title or translation",
    "Unable to find source title",
    "unable to find",
    "Information Unavailable",
    "Information not found",
    "Reported as a violation",
    "None",
    "insufficient information",
    "insufficient verified data",
    "unavailable",
    "No Compound Found",
]


def create_difficulty_ranked_responses(
    metadata_str_dict_by_name: Dict[Tuple[str, str], Dict[str, Any]],
    llms: List[str],
    output_file: str = "response_analysis.md",
) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """
    Create DataFrames with difficulty rankings and difference analysis.
    Applies normalizations in correct order and compares normalized versions.
    """
    # Initialize data structures
    task_responses = defaultdict(int)
    task_response_lists = {}
    task_expected_answers = {}
    task_correct_counts = {}
    task_levels = {}  # New dictionary to store levels

    from mux_py.tests.gaia_scorer import question_scorer, normalize_answer

    def make_hashable(response):
        """Convert response to hashable type"""
        if isinstance(response, list):
            return tuple(response)
        return response

    def process_response(response: str) -> str:
        """Apply all normalizations in correct order"""
        # Step 1: Basic text cleaning
        response = normalize_response(response)

        # Step 2: Normalize for comparison
        normalized_response = normalize_answer(response)

        # Step 3: Check against normalized no_answer_phrases
        normalized_no_answers = [normalize_answer(x) for x in no_answer_phrases]
        if make_hashable(normalized_response) in normalized_no_answers:
            return ""
        # For partial matches, need to convert to string to use 'in'
        if any(
            str(normalize_answer(x)) in str(normalized_response)
            for x in no_answer_phrases
            if len(x) >= 3
        ):
            return ""

        # Step 4: Return normalized hashable version
        return make_hashable(normalized_response)

    # Collect responses for each task
    tasks = set(name for (name, llm) in metadata_str_dict_by_name.keys())
    for task in tasks:
        raw_responses = []
        normalized_responses = []

        # Get expected answer and level
        for llm in llms:
            if (task, llm) in metadata_str_dict_by_name:
                metadata_dict = metadata_str_dict_by_name[(task, llm)]["metadata_dict"]
                expected_answer = metadata_dict["expected_answer"][0][0]
                # Store level
                task_levels[task] = metadata_dict.get("level", "Unknown")
                # Normalize expected answer for comparison
                task_expected_answers[task] = make_hashable(
                    normalize_answer(expected_answer)
                )
                break

        for llm in llms:
            if llm not in ["MODE_LLM", "PASS_ANY"]:
                if (task, llm) in metadata_str_dict_by_name:
                    response = metadata_str_dict_by_name[(task, llm)]["metadata_dict"][
                        "response"
                    ]
                    raw_responses.append(response)  # Store original for display
                    normalized_responses.append(
                        process_response(response)
                    )  # Store normalized for comparison
                else:
                    raw_responses.append("")
                    normalized_responses.append("")

        # Store both raw and normalized responses
        task_response_lists[task] = raw_responses

        # Count unique responses using normalized versions
        unique_responses = set(normalized_responses)  # Now using hashable versions
        task_responses[task] = len(unique_responses)

        # Count correct responses using normalized versions
        expected = task_expected_answers.get(task, "")
        task_correct_counts[task] = sum(
            1 for r in normalized_responses if r and r == expected
        )

    # Assign ranks based on normalized unique counts
    ranks = {}
    for task, responses in task_response_lists.items():
        normalized_responses = [process_response(r) for r in responses]
        if all(r == "" for r in normalized_responses):
            ranks[task] = 0  # All empty responses
        else:
            non_empty_responses = [r for r in normalized_responses if r != ""]
            if not non_empty_responses:
                ranks[task] = 0
            else:
                # Higher rank means more agreement (fewer unique values)
                unique_count = len(set(non_empty_responses))
                empty_count = len(normalized_responses) - len(non_empty_responses)
                valid_llms = [
                    llm for llm in llms if llm not in ["MODE_LLM", "PASS_ANY"]
                ]
                max_possible_rank = len(valid_llms)
                ranks[task] = max_possible_rank - (unique_count + empty_count - 1)
                ranks[task] = max(1, ranks[task])

    import numpy as np

    def calculate_signal_score(responses, max_rank):
        """Score based on maximal variance, treating each empty string as unique."""
        # Replace empty strings with np.nan to ensure they are treated as unique
        processed_responses = [np.nan if r == "" else r for r in responses]

        # Create a set to count unique responses, treating np.nan as unique each time
        unique_responses_count = sum(
            1
            for i, r in enumerate(processed_responses)
            if all(
                r is not np.nan and r != other
                for j, other in enumerate(processed_responses)
                if i != j
            )
            or r is np.nan
        )

        # Diversity ratio: Fraction of responses that are unique
        diversity_ratio = unique_responses_count / len(responses)

        # Scale the score
        score = diversity_ratio * max_rank * 10

        return round(score)

    # Calculate max_rank based on number of valid LLMs
    valid_llms = [llm for llm in llms if llm not in ["MODE_LLM", "PASS_ANY"]]
    max_rank = len(valid_llms)

    # Calculate signal scores for each task
    signal_scores = {}
    for task, responses in task_response_lists.items():
        normalized_responses = [process_response(r) for r in responses]
        signal_scores[task] = round(
            calculate_signal_score(normalized_responses, max_rank)
        )

    df = pd.DataFrame(
        {
            "name": list(task_response_lists.keys()),
            "level": [task_levels[task] for task in task_response_lists.keys()],
            "difficulty_rank": [ranks[task] for task in task_response_lists.keys()],
            "signal_score": [
                signal_scores[task] for task in task_response_lists.keys()
            ],
            "correct_count": [
                task_correct_counts[task] for task in task_response_lists.keys()
            ],
            "rank_minus_correct": [
                ranks[task] - task_correct_counts[task]
                for task in task_response_lists.keys()
            ],
            "expected_answer": [
                task_expected_answers.get(task, "")
                for task in task_response_lists.keys()
            ],
            "responses": [
                [process_response(r) for r in task_response_lists[task]]
                for task in task_response_lists.keys()
            ],
        }
    )

    one_level = False
    if one_level:
        # choose only level 2
        # df = df[df["level"] == '2']
        # only choose if correct_count == 0
        df = df[df["correct_count"] == 0]
        # sort by level even though level is string, treat as integer while sorting
        df["level"] = df["level"].astype(int)
        df = df.sort_values(["level"], ascending=[True])
        # go back to string
        df["level"] = df["level"].astype(str)

        # Create all sorted versions with level column
        df_by_rank = df
        df_by_diff = df

    else:
        # Create all sorted versions with level column
        df_by_rank = df.sort_values("difficulty_rank", ascending=True).reset_index(
            drop=True
        )
        df_by_diff = df.sort_values("rank_minus_correct", ascending=False).reset_index(
            drop=True
        )

    # Update markdown outputs to include level column
    with open(output_file, "w") as f:
        f.write("# Response Analysis by Difficulty Rank\n\n")
        f.write("Generated at: " + str(datetime.now()) + "\n\n")

        f.write("## Summary Statistics\n")
        f.write(f"- Total tasks analyzed: {len(df)}\n")
        f.write(
            f"- Tasks with rank 0 (all empty): {len(df[df['difficulty_rank'] == 0])}\n"
        )
        f.write(f"- Maximum difficulty rank: {df['difficulty_rank'].max()}\n")
        f.write(
            f"- Tasks with any correct answers: {len(df[df['correct_count'] > 0])}\n"
        )
        f.write(
            f"- Average number of correct answers per task: {df['correct_count'].mean():.2f}\n"
        )
        f.write(
            f"- Maximum rank minus correct count: {df['rank_minus_correct'].max()}\n"
        )
        f.write(f"- Average signal score: {df['signal_score'].mean():.2f}\n")
        f.write(f"- Median signal score: {df['signal_score'].median():.2f}\n\n")

        f.write("## Full Analysis Table (Sorted by Difficulty Rank)\n\n")
        df_display = df_by_rank.copy()
        df_display["responses"] = df_display["responses"].apply(lambda x: f"`{str(x)}`")
        df_display["expected_answer"] = df_display["expected_answer"].apply(
            lambda x: f"`{str(x)}`"
        )
        df_display["signal_score"] = df_display["signal_score"].apply(
            lambda x: f"{x:.2f}"
        )
        f.write(df_display.to_markdown(index=False, tablefmt="pipe"))

    # Create signal score version with level
    output_file_signal = output_file.replace(".md", "_signal_score.md")
    with open(output_file_signal, "w") as f:
        f.write("# Response Analysis by Signal Score\n\n")
        f.write("Generated at: " + str(datetime.now()) + "\n\n")

        f.write("## Summary Statistics\n")
        f.write(f"- Total tasks analyzed: {len(df)}\n")
        f.write(
            f"- Tasks with rank 0 (all empty): {len(df[df['difficulty_rank'] == 0])}\n"
        )
        f.write(f"- Maximum difficulty rank: {df['difficulty_rank'].max()}\n")
        f.write(f"- Average signal score: {df['signal_score'].mean():.2f}\n")
        f.write(f"- Median signal score: {df['signal_score'].median():.2f}\n\n")

        f.write("## Full Analysis Table (Sorted by Signal Score)\n\n")

        df_by_signal = df.sort_values("signal_score", ascending=False).reset_index(
            drop=True
        )
        df_display = df_by_signal.copy()
        df_display["responses"] = df_display["responses"].apply(lambda x: f"`{str(x)}`")
        df_display["expected_answer"] = df_display["expected_answer"].apply(
            lambda x: f"`{str(x)}`"
        )
        f.write(df_display.to_markdown(index=False, tablefmt="pipe"))

    # Create simple version with level
    df_simple = df_by_rank[["name", "level", "difficulty_rank", "signal_score"]].copy()

    # Create grouped version
    df_grouped = pd.DataFrame(
        df_simple.groupby(["difficulty_rank", "level"])["name"]
        .apply(list)
        .reset_index()
    )
    df_grouped.columns = ["rank", "level", "names"]
    df_grouped = df_grouped.sort_values(
        ["rank", "level"], ascending=[True, True]
    ).reset_index(drop=True)

    # Create markdown output for grouped version
    output_file_grouped = output_file.replace(".md", "_grouped.md")
    with open(output_file_grouped, "w") as f:
        f.write("# Response Analysis by Difficulty Rank (Grouped)\n\n")
        f.write("Generated at: " + str(datetime.now()) + "\n\n")

        # Write summary statistics
        f.write("## Summary Statistics\n")
        f.write(f"- Total tasks analyzed: {len(df)}\n")
        f.write(f"- Number of different ranks: {len(df_grouped)}\n")
        f.write(
            f"- Tasks with rank 0 (all empty): {len(df[df['difficulty_rank'] == 0])}\n"
        )
        f.write(f"- Maximum difficulty rank: {df['difficulty_rank'].max()}\n\n")

        # Write table showing tasks grouped by rank
        f.write("## Grouped Analysis Table\n")
        df_grouped_display = df_grouped.copy()
        df_grouped_display["names"] = df_grouped_display["names"].apply(
            lambda x: "\n".join([f"- {name}" for name in x])
        )
        f.write(df_grouped_display.to_markdown(index=False))

    # Write simple version to markdown
    output_file_simple = output_file.replace(".md", "_simple.md")
    with open(output_file_simple, "w") as f:
        f.write("# Response Analysis Simple (Difficulty Rank Only)\n\n")
        f.write("Generated at: " + str(datetime.now()) + "\n\n")
        f.write("## Summary Statistics\n")
        f.write(f"- Total tasks analyzed: {len(df_simple)}\n")
        f.write(
            f"- Tasks with rank 0 (all empty): {len(df_simple[df_simple['difficulty_rank'] == 0])}\n"
        )
        f.write(f"- Maximum difficulty rank: {df_simple['difficulty_rank'].max()}\n\n")
        f.write("## Simple Analysis Table\n\n")
        f.write(df_simple.to_markdown(index=False, tablefmt="pipe"))

    # Create difference version with level
    output_file_diff = output_file.replace(".md", "_diff.md")
    with open(output_file_diff, "w") as f:
        f.write("# Response Analysis by Rank-Correct Difference\n\n")
        f.write("Generated at: " + str(datetime.now()) + "\n\n")

        f.write("## Summary Statistics\n")
        f.write(f"- Total tasks analyzed: {len(df)}\n")
        f.write(
            f"- Tasks with rank 0 (all empty): {len(df[df['difficulty_rank'] == 0])}\n"
        )
        f.write(f"- Maximum difficulty rank: {df['difficulty_rank'].max()}\n")
        f.write(
            f"- Tasks with any correct answers: {len(df[df['correct_count'] > 0])}\n"
        )
        f.write(
            f"- Average number of correct answers per task: {df['correct_count'].mean():.2f}\n"
        )
        f.write(
            f"- Maximum rank minus correct count: {df['rank_minus_correct'].max()}\n\n"
        )

        f.write("## Full Analysis Table (Sorted by Rank-Correct Difference)\n")
        df_display = df_by_diff.copy()
        df_display["responses"] = df_display["responses"].apply(lambda x: f"`{str(x)}`")
        df_display["expected_answer"] = df_display["expected_answer"].apply(
            lambda x: f"`{str(x)}`"
        )
        f.write(df_display.to_markdown(index=False, tablefmt="pipe"))

    return df, df_simple, df_grouped


do_validation = os.getenv("DO_VALIDATION", "1") == "1"

if do_validation:
    llm_reference0 = "13626750857"  # best
    llms0 = [
        "13577705545",
        # "13583009468", # bad one at 58%
        "13598752164",
        "13602292431",
        "13604549574",  # 65% docker patch
        "13610686459",  # 63% full docker + gaia minor
        "13626750857",  # 65% extra critique
        "13651636138",  # 60%
        "13693080510",  # 63%
        "13735166151",  # 63%  main
        # "13746608063",  # 41%  no output except browser and unified
        "13752122321",  # 66%  first unified run
        "13769946081",  # 61% no plan
        "13763067042",  # 65%
        # "13774488522",  # 64%
    ]
    # should have _2 etc. for each as derived from merge
    llms_pass = llms0
else:
    llm_reference0 = ""
    llms0 = [
        "13784368905_3",
        "13784386908_3",
        "13818728191_3",
        "13818725909_3",
        "13828053343_3",
        "13829421314_3",
        # "13839811292_3",
        "13839813266_3",
        "13850124407_3",
        "13868254451_3",
        "13875246295_3",
        "13879833780_3",
        "13886198446_3",
        "1111",  # guess
        "12439534033",  # guess
        "12439535203",  # guess
        "12439532788",  # guess
        "1111_2",  # guess
        "12439534033_2",  # guess
        "12439535203_2",  # guess
        "12439532788_2",  # guess
    ]
    # should have _2 etc. for each as derived from merge
    llms_pass = llms0


def extract_innermost_xml_tags(full_text, tags=["name", "page"]):
    results_dict = {k: None for k in tags}
    for tag in tags:
        # First find all opening and closing tag positions
        open_tags = [m.start() for m in re.finditer(rf"<{tag}>", full_text)]
        close_tags = [m.start() for m in re.finditer(rf"</{tag}>", full_text)]

        # Match innermost tags by finding pairs with no tags between them
        innermost_matches = []
        for open_pos in open_tags:
            # Find the closest closing tag after this opening tag
            next_closes = [pos for pos in close_tags if pos > open_pos]
            if next_closes:
                close_pos = min(next_closes)
                # Check if there are no opening tags between this pair
                if not any(open_pos < pos < close_pos for pos in open_tags):
                    # Extract content between tags
                    content_start = open_pos + len(f"<{tag}>")
                    content_end = close_pos
                    innermost_matches.append(full_text[content_start:content_end])

        if innermost_matches:
            results_dict[tag] = innermost_matches[0]  # Take the first innermost match

    return results_dict


@pytest.mark.xfail(raises=FileNotFoundError, strict=False)
def test_pass_rate_e2e():
    do_debug = True

    with open("./test_client.xml", "r") as f:
        bs_data = BeautifulSoup(f.read(), "xml")

    test_suite = bs_data.find("testsuite")
    hostname = test_suite["hostname"]
    test_list = test_suite.find_all("testcase")

    if os.getenv("RUN_GAIA"):
        do_debug = False  # annoying when fails
        from mux_py.tests.gaia_scorer import question_scorer, normalize_answer

        norm_no_answer_phrases = [normalize_answer(x) for x in no_answer_phrases]

        do_validation = os.getenv("DO_VALIDATION", "1") == "1"
        do_merged = os.getenv("DO_MERGED", "0") == "1"
        if do_merged:
            llms = llms_pass  # Use the list of LLMs defined at module level
        else:
            llms = get_llms_for_benchmark()
            llm_reference = "claude-3-7-sonnet-20250219"
            if llm_reference not in llms:
                llm_reference = llms[0]
    else:
        llms = client.get_llm_names()
        # exclude reasoning LLMs from RAG benchmark, too expensive
        reasoning_llms = client.get_reasoning_capable_llm_names()
        llms = [x for x in llms if x not in reasoning_llms]
        llms = sorted(llms, key=lambda x: len(x), reverse=True)
        print(test_list)

    benchmark_data = get_data_for_benchmark()

    times = defaultdict(float)
    passes = defaultdict(int)
    fails = defaultdict(int)
    agent_response_times = defaultdict(list)
    gaia_tool_categories = get_gaia_tool_categories_dict()
    gaia_levels = get_gaia_levels_dict()
    fail_msgs = defaultdict(list)
    fail_questions = defaultdict(int)

    import pandas as pd

    normalize_by_only_annotated = (
        os.getenv("GAIA_NORMALIZE_BY_ONLY_ANNOTATED", "1") == "1"
    )
    if os.getenv("RUN_GAIA"):
        # use test if test set
        if do_validation:
            df_csv = pd.read_csv("parse/tests/gaia_validation_df.csv")
        else:
            df_csv = pd.read_csv("parse/tests/gaia_test_df.csv")
        expecteds_dict = {k: v for k, v in zip(df_csv["name"], df_csv["expecteds"])}

    metadata_str_dict = {}
    metadata_str_dict_by_name = {}

    # First pass: collect all metadata
    for test in test_list:
        test_name = test.get("name")
        if not test_name:
            print(f"Warning: Test case without 'name' attribute: {test}")
            continue
        if "test_mlebench" in test_name:
            print(f"Skipping unrelated test: {test}")
            continue
        if "test_swe_benchmark" in test_name:
            print(f"Skipping unrelated test: {test}")
            continue
        llm = None
        for _llm in llms:
            if _llm + "-" in test_name:
                llm = _llm
                break
        # assert llm is not None, f"{llms} {test_name}"
        if llm is None:
            continue

        dataset = None
        url = None
        for test_row in benchmark_data:
            if test_row[0] + "-" in test_name:
                dataset = test_row[0]
                url = test_row[1]
        assert dataset, f"must find dataset {dataset} in test: {test_name}"

        times[llm] += float(test["time"])
        failure = test.find("failure")

        if os.getenv("RUN_GAIA"):
            metadata_str = read_metadata(llm=llm, task_id=dataset)
            if metadata_str is not None:
                metadata_dict = json.loads(metadata_str)
                metadata_dict["expected_answer"] = ast.literal_eval(
                    expecteds_dict[dataset]
                )
                metadata_str = json.dumps(metadata_dict)
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
            if os.getenv("RUN_GAIA") and not do_validation:
                expected_answer = metadata_dict["expected_answer"][0][0]
                actual_answer = metadata_dict["response"]
                good_answer = question_scorer(actual_answer, expected_answer)
                if not good_answer:
                    # count by annotated questions
                    if (
                        expected_answer == "impossible_1108476_qwerty"
                        and normalize_by_only_annotated
                    ):
                        metadata_str_dict_by_name.pop((dataset, llm))
                        continue
                    else:
                        metadata_str_dict[(test_name, llm)]["failure"] = "Wrong answer"
                    # fail_questions["question"] += 1
                    fails[llm] += 1
                    # fail_msgs[llm].append("Wrong answer")
                else:
                    passes[llm] += 1
            else:
                passes[llm] += 1
        else:
            q, msg = parse_failure_message(failure=failure, dataset=dataset, url=url)
            if os.getenv("RUN_GAIA"):
                q = re.sub(r"([*_\[\]`])", r"\\\1", q)
                q = html.escape(q)
            fail_questions[q] += 1
            fails[llm] += 1
            fail_msgs[llm].append(msg)

    metadata_str_dict_by_name = {
        k: v
        for llm in llms
        for k, v in metadata_str_dict_by_name.items()
        if k[1] == llm
    }

    # Now determine reference LLM if needed
    if os.getenv("RUN_GAIA") and do_merged:
        llm_reference = get_reference_llm(metadata_str_dict_by_name)
        print(
            f"Selected reference LLM {llm_reference} based on mode agreement vs. original llm_reference {llm_reference0}"
        )

        df, df_simple, df_grouped = create_difficulty_ranked_responses(
            metadata_str_dict_by_name, llms
        )

    if os.getenv("NO_SERVER", "0") == "0":
        usage_cost_table = client.get_llm_usage_24h_by_llm()
        perf_table = client.get_llm_performance_by_llm("24 hours")
        llm_vision_map = client.get_llm_and_auto_vision_llm_names()
    else:
        usage_cost_table = []
        perf_table = []
        llm_vision_map = {}

    llm_cost_dict = {u.llm_name: u.llm_cost for u in usage_cost_table}
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

    if os.getenv("NO_SERVER", "0") == "0":
        results_frame = (
            cost_frame.merge(perf_frame, left_on="LLM", right_on="LLM")
            .sort_values(["PASS", "COST"], ascending=[False, True])
            .reset_index(drop=True)
        )
    else:
        results_frame = cost_frame.fillna(0)
        results_frame = results_frame.sort_values(
            ["PASS"], ascending=[False]
        ).reset_index(drop=True)

    results_frame.index = range(1, results_frame.shape[0] + 1)
    results_frame.index.name = "RANK"

    total_pass = results_frame["PASS"].sum()
    total_fail = results_frame["FAIL"].sum()
    accuracy = round((total_pass / (total_pass + total_fail)) * 100, 2)
    row_count = len(results_frame)
    average_pass = round(results_frame["PASS"].mean(), 2)
    average_fail = round(results_frame["FAIL"].mean(), 2)
    average_accuracy = round(results_frame["ACCURACY [%]"].mean(), 2)
    results_summary = pd.DataFrame(
        {
            "Metric": ["Total", "Average"],
            "Model Count": [row_count, "N/A"],
            "PASS": [total_pass, average_pass],
            "FAIL": [total_fail, average_fail],
            "ACCURACY (%)": [accuracy, average_accuracy],
        }
    )

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
            unfiltered_response = orig_response
            if "# filename" in orig_response:
                orig_response = ""
            bad_str = "\n![image]"
            if isinstance(orig_response, str) and bad_str in orig_response:
                index_bad = orig_response.index(bad_str)
                print("cleaned %s" % orig_response)
                orig_response = orig_response[:index_bad]
            orig_response = normalize_response(orig_response)
            if normalize_answer(orig_response) in norm_no_answer_phrases:
                orig_response = ""

            if any(
                normalize_answer(x) in str(normalize_answer(orig_response))
                for x in norm_no_answer_phrases
                if len(x) >= 3
            ):
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
                metadata_str_dict_by_name[(name, "MODE_LLM")]["metadata_dict"][
                    "unfiltered_response"
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
                metadata_str_dict_by_name[(name, "MODE_LLM")]["metadata_dict"][
                    "unfiltered_response"
                ].append(unfiltered_response)
        submission = []
        submission_labels = []
        good_answer_count = 0
        for (name, llm), metadata_str_dict1 in metadata_str_dict_by_name.copy().items():
            if do_merged:
                # COMPARE TWO LLMs
                # llm_good = "12335677182_2"
                # llm_bad1 = "12341495681"
                # llm_bad2 = "12341495681_2"

                # llm_good = "12335677182"
                # llm_bad1 = "12341495681"
                # llm_bad2 = "12341495681"

                # level 2 check
                llm_good = "13850124407"
                llm_bad1 = "13875246295"
                llm_bad2 = "13868254451"

                if llm == llm_good:
                    orig_response_good = metadata_str_dict_by_name[(name, llm)][
                        "metadata_dict"
                    ]["response"]
                    if (name, llm_bad1) in metadata_str_dict_by_name:
                        orig_response_bad = metadata_str_dict_by_name[(name, llm_bad1)][
                            "metadata_dict"
                        ]["response"]
                    else:
                        orig_response_bad = "FAILED"
                    if (name, llm_bad2) in metadata_str_dict_by_name:
                        orig_response_bad2 = metadata_str_dict_by_name[
                            (name, llm_bad2)
                        ]["metadata_dict"]["response"]
                    else:
                        orig_response_bad2 = "FAILED"
                    expected_now = metadata_str_dict_by_name[(name, llm)][
                        "metadata_dict"
                    ]["expected_answer"]
                    good_answer_good = question_scorer(
                        orig_response_good, expected_now[0][0]
                    )
                    good_answer_bad = question_scorer(
                        orig_response_bad, expected_now[0][0]
                    )
                    good_answer_bad2 = question_scorer(
                        orig_response_bad2, expected_now[0][0]
                    )
                    if (
                        good_answer_good
                        and not good_answer_bad
                        and not good_answer_bad2
                    ):
                        orig_response_good = orig_response_good.replace("\n", "")[0:15]
                        orig_response_bad = orig_response_bad.replace("\n", "")[0:15]
                        orig_response_bad2 = orig_response_bad2.replace("\n", "")[0:15]
                        print(
                            f"\nname:{name} (exp: {expected_now[0][0]}) good_answer_good: {good_answer_good} ({orig_response_good}) good_answer_bad: {good_answer_bad} ({orig_response_bad}) good_answer_bad2: {good_answer_bad2} ({orig_response_bad2})",
                            file=sys.stdout,
                        )

            if llm == "MODE_LLM":
                response = metadata_str_dict_by_name[(name, "MODE_LLM")][
                    "metadata_dict"
                ]["response"]
                orig_response = metadata_str_dict_by_name[(name, "MODE_LLM")][
                    "metadata_dict"
                ]["orig_response"]
                have_ref = (name, llm_reference) in metadata_str_dict_by_name
                if have_ref:
                    response_reference = metadata_str_dict_by_name[
                        (name, llm_reference)
                    ]["metadata_dict"]["response"]
                    response_reference = normalize_response(response_reference)

                    if normalize_answer(response_reference) in norm_no_answer_phrases:
                        response_reference = ""
                    if any(
                        normalize_answer(x) in str(normalize_answer(response_reference))
                        for x in norm_no_answer_phrases
                        if len(x) >= 3
                    ):
                        response_reference = ""

                    response_reference = normalize_response(response_reference)
                else:
                    response_reference = ""

                expected = metadata_str_dict_by_name[(name, "MODE_LLM")][
                    "metadata_dict"
                ]["expected_answer"]

                response_list = response  # DEBUG
                if response:
                    print(
                        f"\nname: {name} expected: {expected[0][0]} for response_list: {response_list}",
                        file=sys.stdout,
                    )
                    if len(set(response)) == len(response) and response_reference:
                        # if nobody agrees, go with smartest model
                        response = response_reference
                    else:
                        response = most_common_or_default(response, response_reference)
                        if isinstance(response, tuple):
                            response = ", ".join(map(str, response))
                        else:
                            response = str(response)
                else:
                    response = ""
                if not response:
                    print(f"no response: {name}", file=sys.stdout)
                    print(
                        "unfiltered responses: %s"
                        % metadata_str_dict_by_name[(name, "MODE_LLM")][
                            "metadata_dict"
                        ]["unfiltered_response"]
                    )
                good_answer0 = question_scorer(orig_response, expected[0][0])
                good_answer = question_scorer(response, expected[0][0])
                any_good_answer = any(
                    [
                        question_scorer(
                            str(x)
                            if not isinstance(x, tuple)
                            else ", ".join(map(str, x)),
                            expected[0][0],
                        )
                        for x in response_list
                    ]
                )
                submission.append(dict(task_id=name, model_answer=response))
                submission_labels.append(
                    dict(
                        task_id=name,
                        model_answer=response,
                        good_answer=good_answer,
                        good_answer0=good_answer0,
                        expected=expected[0][0],
                    )
                )

                # DEBUG:
                if response_reference is not None:
                    good_reference = question_scorer(response_reference, expected[0][0])
                    reference_good_mode_bad = not good_answer and good_reference
                    reference_bad_mode_good = good_answer and not good_reference
                    if reference_good_mode_bad:
                        print(
                            f"reference_good_mode_bad: {name} {response_list}",
                            file=sys.stdout,
                        )
                    if reference_bad_mode_good:
                        print(
                            f"reference_bad_mode_good: {name} {response_list} {response_reference}",
                            file=sys.stdout,
                        )
                else:
                    print("Reference not found: %s" % name)
                if (
                    not good_answer
                    and not metadata_str_dict_by_name[(name, "PASS_ANY")]["failure"]
                ):
                    print(
                        "MODE BAD but one good: %s expected: %s: list: %s"
                        % (name, expected[0][0], response_list)
                    )
                if not good_answer and any_good_answer:
                    print(
                        "2 MODE BAD but one good: %s expected: %s: list: %s"
                        % (name, expected[0][0], response_list)
                    )
                if good_answer and not any_good_answer:
                    print(
                        "3 MODE GOOD but none good: %s expected: %s: list: %s"
                        % (name, expected[0][0], response_list)
                    )

                checking_one = False
                if checking_one:
                    if good_answer0 != good_answer:
                        print(name)
                good_answer_count += 1 if good_answer else 0
                pass_any_failure = metadata_str_dict_by_name[(name, "PASS_ANY")][
                    "failure"
                ]
                print(
                    f"GOODMODELLM: {1 if good_answer else 0} {pass_any_failure} {response}, {expected[0][0]}"
                )
                metadata_str_dict_by_name[(name, "MODE_LLM")]["failure"] = (
                    None if good_answer else "failure"
                )
        print(f"good_answer_count: {good_answer_count}")
        llms.append("MODE_LLM")
        if submission:
            if not do_validation:
                # add dummy problem back in
                submission.append(dict(task_id="0-0-0-0-0", model_answer="?"))
                submission_labels.append(
                    dict(
                        task_id="0-0-0-0-0",
                        model_answer="?",
                        good_answer=True,
                        good_answer0=True,
                        expected="?",
                    )
                )

            if do_debug:
                if do_validation:
                    assert len(submission) == 165
                else:
                    assert len(submission) == 301
            type_str = "validation" if do_validation else "test"
            with open(f"submission_{type_str}.jsonl", "wt", encoding="utf-8") as f:
                for s in submission:
                    f.write(json.dumps(s, ensure_ascii=False) + "\n")
            with open(
                f"submission_labels_{type_str}.jsonl", "wt", encoding="utf-8"
            ) as f:
                for s in submission_labels:
                    f.write(json.dumps(s, ensure_ascii=False) + "\n")

        for (name, llm), metadata_str_dict1 in metadata_str_dict_by_name.items():
            metadata_dict = metadata_str_dict1["metadata_dict"]
            failure = metadata_str_dict1["failure"]
            level_meta = metadata_dict.get("level")

            if llm not in ["PASS_ANY", "MODE_LLM"]:
                expected_answer = metadata_dict["expected_answer"][0][0]
                actual_answer = metadata_dict["response"]
                good_answer = question_scorer(actual_answer, expected_answer)
                if failure is None and not good_answer:
                    failure = "failure"

            df2 = df_csv[df_csv["name"] == name]
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
    if not os.getenv("SMOKE") and do_debug:
        assert skipped <= 2, (
            f"too many tests skipped (should only skip agents+swe)."
            f"make sure to run `make test_client_e2e` with TEST_ALL=1: skipped={skipped}"
        )
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
        f.write(f"\n## Results Summary:\n")
        f.write(results_summary.to_markdown())
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


@pytest.mark.skipif(os.getenv("DO_MERGED", "0") == "0", reason="Only merge if should")
def test_merge_many_runs():
    import os
    import tarfile
    import zipfile
    import shutil
    import json
    from pathlib import Path
    from bs4 import BeautifulSoup

    # Input parameters
    target_llm_names = [
        "claude-3-5-sonnet-20240620",
        "claude-3-5-sonnet-20241022",
        "claude-3-7-sonnet-20250219",
        # "deepseek-ai/DeepSeek-V3",
        # "deepseek-ai/DeepSeek-V3-together",
    ]  # Only include data for this LLM
    do_validation = os.getenv("DO_VALIDATION", "1") == "1"

    # cd ~/Downloads/all_main_gaia
    # bash ~/h2ogpte/scripts/gaia_artifacts_download_1.sh 12110242173

    # cd ~/Downloads/all_main_gaia
    # bash ~/h2ogpte/scripts/gaia_artifacts_download.sh

    # cd ~/h2ogpte/
    # NO_SERVER=1 RUN_GAIA=1 pytest -s -v mux_py/tests/test_benchmarks::test_merge_many_runs
    # TEST_ALL=1 RUN_GAIA=1 DO_MERGED=1 NO_SERVER=1 pytest -s -v mux_py/tests/test_benchmarks::test_pass_rate_e2e &> pass1.log

    if do_validation:
        artifacts_dir = "/home/jon/Downloads/all_main_gaia/artifacts"
    else:
        artifacts_dir = "/home/jon/Downloads/all_main_gaia/test_artifacts"
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
        if not any(x in run_dir for x in llms0):
            continue

        run_id = os.path.basename(run_dir)  # Use run_id as the label

        for target_llm_name in target_llm_names:
            llm_label_index = target_llm_names.index(target_llm_name) + 1
            if llm_label_index > 1:
                llm_label_with_index = f"{run_id}_{llm_label_index}"
            else:
                llm_label_with_index = f"{run_id}"

            print(
                f"Processing {target_llm_name} for artifacts in {run_dir} as {llm_label_with_index}..."
            )

            # Create a temporary directory to extract files
            temp_dir = os.path.join(temp_output_dir, f"temp_{llm_label_with_index}")
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
            copy_meta_data_files(
                gaia_folder, target_llm_name, llm_label_with_index, base_output_dir
            )

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

                test_suite[
                    "hostname"
                ] = llm_label_with_index  # Update hostname to include index
                for test_case in test_suite.find_all("testcase"):
                    # Include only test cases for the specific LLM
                    if target_llm_name in test_case.get("name", ""):
                        test_case["name"] = test_case["name"].replace(
                            target_llm_name, llm_label_with_index
                        )  # Rename LLM in test case name
                        merged_test_cases.append(test_case)

            # Clean up temporary directory
            shutil.rmtree(temp_dir)

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
