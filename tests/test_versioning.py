from importlib.metadata import version

from packaging.version import Version

import ordin


LAST_PUBLISHED_VERSION = "0.1.0"


def test_runtime_version_matches_installed_distribution():
    assert ordin.__version__ == version("ordin")


def test_post_release_source_does_not_reuse_published_version():
    current = Version(ordin.__version__)
    previous = Version(LAST_PUBLISHED_VERSION)
    assert str(current) == ordin.__version__
    assert len(current.release) == 3
    assert current.release > previous.release
    assert current.pre is None
    assert current.post is None
    assert current.local is None
