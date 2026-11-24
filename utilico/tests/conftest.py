import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import pytest  # noqa: E402

from app import guard  # noqa: E402

guard.set_offline_env()
guard.install()

DATA_DIR = os.environ.get("UTILICO_DATA_DIR", r"C:\sources\Utilco\data")


@pytest.fixture(scope="session")
def registry():
    from app.datasource import Registry
    return Registry(DATA_DIR)


@pytest.fixture(scope="session")
def kb():
    from app.config import Config
    from app.knowledge import KnowledgeBase
    return KnowledgeBase(Config())
