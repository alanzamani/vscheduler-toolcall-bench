from pathlib import Path

import pytest

from toolbench import tools as tools_mod
from toolbench.cases import load_cases

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = Path(__file__).with_name("fixtures") / "tools.json"


@pytest.fixture(scope="session")
def tools():
    return tools_mod.load(FIXTURE)


@pytest.fixture(scope="session")
def tool_names(tools):
    return {t.name for t in tools}


@pytest.fixture(scope="session")
def specs():
    return tools_mod.load_specs()


@pytest.fixture(scope="session")
def cases(tool_names, specs):
    return load_cases(ROOT / "cases", tool_names, specs)
