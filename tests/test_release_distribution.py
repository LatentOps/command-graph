from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_release_workflow_publishes_only_to_github():
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    lowered = workflow.lower()

    assert "publish-pypi" not in lowered
    assert "gh-action-pypi-publish" not in lowered
    assert "pypi_publish" not in lowered
    assert "Publish GitHub Release" in workflow
    assert 'gh release create "$GITHUB_REF_NAME"' in workflow
    assert "contents: write" in workflow
    assert "refusing to replace published assets" in workflow


def test_installation_paths_are_github_native():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    installation = (ROOT / "docs" / "installation.md").read_text(encoding="utf-8")
    releasing = (ROOT / "docs" / "releasing.md").read_text(encoding="utf-8")

    stable_install = "git+https://github.com/LatentOps/ordin.git@v0.2.0"
    development_install = "git+https://github.com/LatentOps/ordin.git"

    assert stable_install in readme
    assert stable_install in installation
    assert stable_install in releasing
    assert development_install in readme
    assert development_install in installation
    assert development_install in releasing

    for document in (readme, installation, releasing):
        assert "pypi" not in document.lower()
