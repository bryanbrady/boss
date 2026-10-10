import pytest


def pytest_addoption(parser):
    parser.addoption("--docker", action="store_true", help="run tests that need Docker")


def pytest_collection_modifyitems(config, items):
    if config.getoption("--docker"):
        return
    skip = pytest.mark.skip(reason="needs --docker")
    for item in items:
        if "docker" in item.keywords:
            item.add_marker(skip)
