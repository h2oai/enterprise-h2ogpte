import ast
import json
import os
import shutil
import sys
import time
import uuid
from typing import Union, Any
from agents_test_utils import (
    get_agents_chat_history,
    sanitize,
    get_agent_files,
    get_agents_chat_history_md,
    get_agents_analysis,
    convert_markdown_to_html,
)
import pytest

from conftest import (
    e2e_data,
    gaia_data,
    set_num_openblas_threads,
    reasoning_models_to_keep,
)
from mux_py.tests.test_benchmarks_utils import get_gaia_specific_prompt
from mux_py.tests.test_benchmarks_client import client
from mux_py.tests.test_benchmarks_enums import timestamp, GAIA_RESULTS_DIR
from parse.tests.datasets_cached import CachedFile
from mux_py.tests.test_mux import get_test_name

set_num_openblas_threads(1)

assert e2e_data, "Must have Q&A RAG dataset."
assert gaia_data, "Must have GAIA dataset"

from mux_py.tests.agents_test_utils import agent_llms


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
                    # Determine if this is a validation_with_steps case
                    do_validation = os.getenv("DO_VALIDATION", "1") == "1"
                    validation_with_steps = (
                        do_validation and os.getenv("VALIDATION_WITH_STEPS", "0") == "1"
                    )

                    # Get the prompt, passing name as task_id and validation_with_steps flag
                    prompt = get_gaia_specific_prompt(
                        task_id=name, validation_with_steps=validation_with_steps
                    )

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
                            "max_time": 3600 * 2,
                            "agent_total_timeout": 3000 * 2,
                            "agent_timeout": 60 * 10,  # try 10 minutes
                            "agent_tools": agent_tools,
                            "temperature": 0.6
                            if "DeepSeek-R1" in llm and os.getenv("RUN_GAIA")
                            else 0.0,
                            "agent_planning_forced_mode": True,
                            # "agent_main_model": "deepseek-ai/DeepSeek-R1-shadeform",
                            # "agent_main_model": "o3",
                            "agent_max_confidence_level": 2,
                            "agent_main_reasoning_effort": 10000,
                            "agent_advanced_reasoning_effort": 20000,
                            "agent_code_restrictions_level": 0,
                        },
                        rag_config={
                            "rag_type": "llm_only",
                        },
                        timeout=4000 * 2,
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
