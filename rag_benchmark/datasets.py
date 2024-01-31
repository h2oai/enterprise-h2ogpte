from pathlib import Path
from typing import Dict

import pandas as pd
import requests

root = Path(__file__).parent


# This class inherits from `str` in order to trick pytest into using the `name`
# portion of this class when generating a parameter name in a parametrized test.
class CachedFile(str):
    ALL_FILES: Dict[str, str] = {}

    def __new__(cls, name: str, url: str):
        return super().__new__(cls, name)

    def __init__(self, name: str, url: str):
        filename = url.rsplit("/", 1)[1]
        if filename in CachedFile.ALL_FILES:
            filename = name + "-" + filename
            CachedFile.ALL_FILES[filename] = url

        self.url = url
        self._path = root / "data" / "cached" / filename

    def get(self) -> Path:
        """Returns the path to the file, downloading it if necessary."""
        if not self._path.exists():
            self._path.parent.mkdir(parents=True, exist_ok=True)
            res = requests.get(self.url)
            res.raise_for_status()  # raises an error if one occurred
            self._path.write_bytes(res.content)

        assert self._path.is_file()
        return self._path


femsa = CachedFile(
    "Femsa",
    "https://enterprise-h2ogpt-public-data.s3.amazonaws.com/Coca-Cola-FEMSA-Results-1Q23-vf-2.pdf",
)
wells_fargo = CachedFile(
    "WellsFargo",
    "https://enterprise-h2ogpt-public-data.s3.amazonaws.com/wellsfargo-2022-annual-report.pdf",
)
citi_report = CachedFile(
    "CitiAnnual",
    "https://enterprise-h2ogpt-public-data.s3.amazonaws.com/citi-2022-annual-report.pdf",
)
kaiser = CachedFile(
    "Kaiser",
    "https://enterprise-h2ogpt-public-data.s3.amazonaws.com/kp-annual-report-en-2019.pdf",
)
cba = CachedFile(
    "CBA-Spreads",
    "https://enterprise-h2ogpt-public-data.s3.amazonaws.com/2023-Annual-Report-Spreads.pdf",
)
cba_fullpage = CachedFile(
    "CBA-Annual",
    "https://enterprise-h2ogpt-public-data.s3.amazonaws.com/CBA.2023.Annual.Report.pdf",
)
cba_wheel = CachedFile(
    "CBA-Wheel",
    "https://enterprise-h2ogpt-public-data.s3.amazonaws.com/CBA-1H23-Results-Presentation_wheel.pdf",
)
nyl_all = CachedFile(
    "NYL_All",
    "https://enterprise-h2ogpt-public-data.s3.amazonaws.com/2022-nyl-investment-report.pdf",
)
bradesco = CachedFile(
    "Bradesco",
    "https://enterprise-h2ogpt-public-data.s3.amazonaws.com/bradesco-2022-integrated-report.pdf",
)
tabasco = CachedFile(
    "Tabasco",
    "https://enterprise-h2ogpt-public-data.s3.amazonaws.com/Tabasco_Ingredients_Products_Guide.pdf",
)
citi_report_pg6 = CachedFile(
    "Citi6",
    "https://enterprise-h2ogpt-public-data.s3.amazonaws.com/citi-2022-annual-report-page6.pdf",
)
citi_report_pg1_2 = CachedFile(
    "Citi1_2",
    "https://enterprise-h2ogpt-public-data.s3.amazonaws.com/citi-2022-annual-report-pages1-2.pdf",
)
nyl_report_pg5_15 = CachedFile(
    "NYL5_15",
    "https://enterprise-h2ogpt-public-data.s3.amazonaws.com/2022-nyl-investment-report-pages-5-and-15.pdf",
)
aluminum_int = CachedFile(
    "AluminumInt",
    "https://enterprise-h2ogpt-public-data.s3.amazonaws.com/Aluminum.Intelligence.Report.November.2022.pdf",
)
albumentations_markdown = CachedFile(
    "AlbumentationsREADME",
    "https://enterprise-h2ogpt-public-data.s3.amazonaws.com/albumentations-README.md",
)
best_buy = CachedFile(
    "BestBuy",
    "https://enterprise-h2ogpt-public-data.s3.amazonaws.com/Best-Buy-Investor-Event-March-2022.pdf",
)

example_rst = CachedFile(
    "ExampleRST",
    "https://enterprise-h2ogpt-public-data.s3.amazonaws.com/example-rst2.rst",
)

audio_label_genie = CachedFile(
    "AudioLabelGenie",
    "https://enterprise-h2ogpt-public-data.s3.amazonaws.com/label-genie-intro-youtube.mp3",
)

fast_food = CachedFile(
    "FastFood",
    "https://enterprise-h2ogpt-public-data.s3.amazonaws.com/fastfood.jpg",
)

sanepar_pg4 = CachedFile(
    "Sanepar_4",
    "https://enterprise-h2ogpt-public-data.s3.amazonaws.com/Demonstracoes-Financeiras-Anuaanepar-2022-12-31-gmdgFjGq-page4.pdf",
)

dell_scanned_pr = CachedFile(
    "dell_scanned_pr",
    "https://enterprise-h2ogpt-public-data.s3.amazonaws.com/Q2 FY24 Financial Results Press Release.pdf",
)

jpeg_xr_image = CachedFile(
    "JPEG-XR",
    "https://enterprise-h2ogpt-public-data.s3.amazonaws.com/gilgamesh_tablet_1.jxr",
)

# Check naming of CachedFile objects, first arguments must be unique enough to be grep'able for e2e benchmarks.
g = globals().copy()
for i in g:
    gi = g[i]
    if not isinstance(gi, CachedFile):
        continue
    for j in g:
        gj = g[j]
        if gj == gi:
            continue
        if not isinstance(gj, CachedFile):
            continue
        assert str(gi) not in str(gj), (
            f"Invalid naming: {str(gi)} is contained in {str(gj)}. "
            f"The name attribute (first argument) of any of the CachedFile instances must not "
            "be contained in another object's name."
        )


e2e_df = pd.read_csv(f"./e2e_df.csv")


def check_names(df):
    for i in df["name"]:
        for j in df["name"]:
            if j == i:
                continue
            assert i not in j, (
                f"Invalid naming: {i} is contained in {j}. "
                f"The name attribute (first argument) must not "
                "be contained in another object's name."
            )


check_names(e2e_df)
