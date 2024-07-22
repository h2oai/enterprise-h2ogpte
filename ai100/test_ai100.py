import io
import json
import os
import shutil
from collections import Counter
from typing import Union

import filelock
import datatable as dt
import pytest

from h2ogpte import H2OGPTE
from conftest import ai100_data

from mux_py.tests.test_mux import REMOTE_ADDRESS, API_KEY

assert REMOTE_ADDRESS, "Must have h2oGPTe remote server address."
assert API_KEY, "Must set H2OGPTE_API_KEY env var first."

client = H2OGPTE(address=REMOTE_ADDRESS, api_key=API_KEY)


@pytest.mark.skip("developers only")
@pytest.mark.parametrize(
    "llm",
    [
        "claude-3-5-sonnet-20240620",
        "gpt-4o",
    ],
)
@pytest.mark.parametrize(
    "row, firstname, lastname, name, title, company, text, what, hq, usecase",
    ai100_data,
)
def test_ai100_score(
    row: int,
    firstname: str,
    lastname: str,
    name: str,
    title: str,
    company: str,
    text: str,
    what: str,
    hq: str,
    usecase: str,
    llm: Union[str, int, None],
):
    import numpy as np

    if any(x in [None, np.nan, ""] for x in [text]) or "no info" in text.lower():
        chat_session_id = client.create_chat_session()
        with client.connect(chat_session_id) as session:
            text = session.query(
                message=f"What achievements in AI has {name} at {company} demonstrated?",
                timeout=200,
                llm=llm,
            ).content

    collection_name = "Collection for %s" % name
    collection_id = client.create_collection(
        name=collection_name,
        description=collection_name,
    )
    # upload text column by itself
    upload_id = client.upload(name + ".txt", io.StringIO(text))
    client.ingest_uploads(
        collection_id,
        [upload_id],
        gen_doc_summaries=False,
        gen_doc_questions=False,
        ocr_model="off",
    )

    system_prompt = """
            You are a fair and insightful judge, expertised in judging the impact of AI executives in their field.
            Your goal is to find the top AI executives in the world. There are over 100 candidates.
            You will judge each candidate based on a short description and your existing knowledge about them.

            Candidates get extra points by showing any significant achievements across these dimensions:
            * Business Impact: Demonstrable outcomes (e.g., quantifiable metrics on revenue, model usage, organization-wide adoption)
            * Innovation: Novel approaches (e.g., proprietary models, unique architectures beyond standard implementations)
            * Leadership: Widespread implementation (e.g., beyond pilot stage, actively deployed across teams)
            * Scalability: Extensive deployment (e.g., broad implementation across the organization)

            Pay attention to the evidence below, and score the candidate's achievements with a number of 1 (average), 2 (outstanding) or 3 (exceptional) based on the criteria above.
            Scores should be distributed roughly uniformly, but scores of 3 should be especially rare.
            """

    chat_session_id = client.create_chat_session(collection_id)
    with client.connect(chat_session_id) as session:
        reply = session.query(
            system_prompt=system_prompt,
            prompt_query=f"""{system_prompt}
            According to your prior knowledge about the relevant achievements by {name} at {company}, and according to the context provided above (when available and useful), 
            """,
            message="Provide a score of either 1 (average), 2 (outstanding) or 3 (exceptional) for each criterion. Make sure to hand out these scores roughly evenly across candidates.",
            timeout=200,
            llm=llm,
            rag_config={"rag_type": "all_data"},
        )
        print(reply.content)
        file = "parse/tests/ai100_df.csv"
        file_copy = "parse/tests/ai100_df_results.csv"
        reason_col = f"reasoning_{llm}"
        score_col = f"score_{llm}"
        lock_file = "ai100.lock.tmp"
        with filelock.FileLock(lock_file):
            if not os.path.exists(file_copy):
                shutil.copy(file, file_copy)
                ai100_df = dt.fread(file_copy)
                ai100_df.to_csv(file_copy)
            ai100_df = dt.fread(file_copy)
            if reason_col not in ai100_df.names:
                ai100_df[reason_col] = "N/A"
            if score_col not in ai100_df.names:
                ai100_df[score_col] = 0
            if "row" not in ai100_df.names:
                ai100_df["row"] = "N/A"
            ai100_df[row, reason_col] = reply.content
            ai100_df["row"] = str
            ai100_df[row, "row"] = str(row)
            ai100_df.to_csv(file_copy)

        reply = session.query(
            system_prompt="",
            pre_prompt_query="",
            prompt_query="",
            message="Now average the scores above, and round to nearest integers 1, 2 or 3. Answer with just the score like this: {'score': 2}",
            include_chat_history=True,
            timeout=200,
            llm=llm,
            llm_args=dict(
                guided_json={
                    "type": "object",
                    "properties": {"score": {"type": "integer", "enum": [1, 2, 3]}},
                    "required": ["score"],
                }
            ),
            rag_config=dict(rag_type="llm_only"),
        )
        print(reply.content)
        score = json.loads(reply.content)["score"]
        assert score in [1, 2, 3]

        with filelock.FileLock(lock_file):
            ai100_df = dt.fread(file_copy)
            ai100_df[score_col] = int
            ai100_df[row, score_col] = score
            ai100_df.to_csv(file_copy)


@pytest.mark.skip("developers only")
@pytest.mark.parametrize(
    "llm",
    [
        "claude-3-5-sonnet-20240620",
    ],
)
@pytest.mark.parametrize(
    "row, firstname, lastname, name, title, company, text, what, hq, usecase",
    ai100_data,
)
def test_ai100_summary(
    row: int,
    firstname: str,
    lastname: str,
    name: str,
    title: str,
    company: str,
    text: str,
    what: str,
    hq: str,
    usecase: str,
    llm: Union[str, int, None],
):
    import numpy as np

    if any(x in [None, np.nan, ""] for x in [text]) or "no info" in text.lower():
        chat_session_id = client.create_chat_session()
        with client.connect(chat_session_id) as session:
            text = session.query(
                message=f"What achievements in AI has {name} at {company} demonstrated?",
                timeout=200,
                llm=llm,
            ).content

    collection_name = "Collection for %s" % name
    collection_id = client.create_collection(
        name=collection_name,
        description=collection_name,
    )
    # upload text column by itself
    upload_id = client.upload(name + ".txt", io.StringIO(text))
    client.ingest_uploads(
        collection_id,
        [upload_id],
        gen_doc_summaries=False,
        gen_doc_questions=False,
        ocr_model="off",
    )

    system_prompt = """
            You are an expert summarizer, creating concise 5 to 10 word summaries.
            
            You must create a short summary for each AI executive, highlighthing their main achievements.
            
            Examples: 
            * Improved innovation through fast prototyping using GenAI
            * Created virtual assistant that serves 10 million users
            * Automated drug discovery through advanced AI
            
            """

    chat_session_id = client.create_chat_session(collection_id)
    with client.connect(chat_session_id) as session:
        reply = session.query(
            system_prompt=system_prompt,
            prompt_query=f"""{system_prompt}
            According to the context provided above,
            """,
            message="Create a short 1-liner summary highlighting the main achievements.",
            timeout=200,
            llm=llm,
            rag_config={"rag_type": "all_data"},
        )
        print(reply.content)
        file = "parse/tests/ai100_df.csv"
        file_copy = "parse/tests/ai100_df_summary.csv"
        summary_col = f"summary"
        lock_file = "ai100.lock.tmp"
        with filelock.FileLock(lock_file):
            if not os.path.exists(file_copy):
                shutil.copy(file, file_copy)
                ai100_df = dt.fread(file_copy)
                ai100_df.to_csv(file_copy)
            ai100_df = dt.fread(file_copy)
            if summary_col not in ai100_df.names:
                ai100_df[summary_col] = "N/A"
            if "row" not in ai100_df.names:
                ai100_df["row"] = "N/A"
            ai100_df[row, summary_col] = reply.content
            ai100_df["row"] = str
            ai100_df[row, "row"] = str(row)
            ai100_df.to_csv(file_copy)


def test_get_ai100():
    df = dt.fread("parse/tests/ai100_df_results.csv")
    names_counters = Counter(df[dt.f["score_gpt-4o"] != 1, "Name"].to_list()[0])
    print(names_counters.most_common())
    names1 = set(df[dt.f["score_gpt-4o"] != 1, "Name"].to_list()[0])
    names2 = set(df[dt.f["score_claude-3-5-sonnet-20240620"] != 1, "Name"].to_list()[0])
    print(f"intersection: {len(names1.intersection(names2))}")
    print(f"intersection: {(names1.intersection(names2))}")
    print(f"symmetric difference: {len(names1.symmetric_difference(names2))}")
