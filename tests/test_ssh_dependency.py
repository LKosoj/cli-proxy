"""Test that asyncssh dependency is installed and importable."""

import importlib


def test_asyncssh_importable():
    mod = importlib.import_module("asyncssh")
    assert hasattr(mod, "connect")
    assert hasattr(mod, "generate_private_key")


def test_asyncssh_version_range():
    import asyncssh

    parts = asyncssh.__version__.split(".")
    major = int(parts[0])
    minor = int(parts[1])
    assert major == 2 and minor >= 14, (
        f"asyncssh {asyncssh.__version__} outside >=2.14.0,<3.0.0"
    )
