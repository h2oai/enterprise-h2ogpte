import os
from typing import List, Set, Tuple
import pandas as pd
import pytest

# Global variables to hold CSV data
summarize_data: List[Tuple[str, str]] = []
e2e_data: List[Tuple[str, str, str, str, bool]] = []


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--e2e_csvfile",
        action="store",
        default="./e2e_df.csv",
        help="Path to the csv file for e2e testing",
    )


def pytest_configure(config: pytest.Config) -> None:
    global e2e_data
    e2e_csv_file = config.getoption("e2e_csvfile")
    if e2e_csv_file:
        e2e_df = pd.read_csv(e2e_csv_file)
        e2e_data = [tuple(x) for x in e2e_df.to_numpy()]
        if os.getenv("INGEST_ONLY"):
            # for ingest job, only need each file once
            unique_data: List[Tuple[str, str, str, str, bool]] = []
            which: Set[str] = set()
            for tup in e2e_data:
                if tup[0] in which:
                    continue
                else:
                    which.add(tup[0])
                    unique_data.append(tup)
            e2e_data = unique_data


    # Register markers
    config.addinivalue_line("markers", "e2e")
