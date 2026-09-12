import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from ordin.cli import main
from ordin.shell_integration import render_shell_init


def test_bash_init_is_explicit_reversible_and_has_no_eval():
    script = render_shell_init("bash")
    assert "orun()" in script
    assert "ordin_shell_disable()" in script
    assert "command bash -c" in script
    assert "eval " not in script
    assert '--cwd "$PWD"' in script
    assert "--interactive" in script


def test_zsh_init_includes_review_accept_widget_without_rebinding_enter():
    script = render_shell_init("zsh")
    assert "ordin-review-accept" in script
    assert "^X^G" in script
    assert "zle .accept-line" in script
    assert "ordin_shell_disable()" in script
    assert "eval " not in script
    assert "bindkey '^M'" not in script


def test_shell_init_cli_prints_script(capsys):
    assert main(["shell-init", "bash"]) == 0
    output = capsys.readouterr().out
    assert output == render_shell_init("bash")


def test_unsupported_shell_is_rejected():
    with pytest.raises(ValueError):
        render_shell_init("fish")


def _fake_ordin(path: Path, *, decision: str, exit_code: int) -> None:
    path.write_text(
        f"#!/bin/sh\nprintf 'decision: {decision}\\nrisk: low\\n'\nexit {exit_code}\n",
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


@pytest.fixture(params=["bash", "zsh"])
def shell(request):
    if os.name != "posix":
        pytest.skip("shell execution fixtures require POSIX paths and executable modes")
    name = request.param
    executable = f"/bin/{name}" if sys.platform == "darwin" else shutil.which(name)
    if executable is None or not Path(executable).is_file():
        pytest.skip(f"{name} is unavailable")
    return name, executable


def test_wrapper_executes_allowed_exact_text(tmp_path, shell):
    name, executable = shell

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _fake_ordin(bin_dir / "ordin", decision="allow", exit_code=0)
    init_file = tmp_path / "ordin init.sh"
    init_file.write_text(render_shell_init(name), encoding="utf-8")

    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    result = subprocess.run(
        [
            executable,
            "-c",
            'source "$1"; orun "$2"',
            "ordin-test",
            str(init_file),
            "printf one\nprintf two",
        ],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )
    assert result.returncode == 0
    assert result.stdout == "onetwo"


@pytest.mark.parametrize("exit_code", [10, 20, 30, 127])
def test_wrapper_does_not_execute_rejected_text(tmp_path, shell, exit_code):
    name, executable = shell

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _fake_ordin(bin_dir / "ordin", decision="block", exit_code=exit_code)
    init_file = tmp_path / "ordin init.sh"
    init_file.write_text(render_shell_init(name), encoding="utf-8")
    blocked_path = tmp_path / "must-not-exist"

    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    result = subprocess.run(
        [
            executable,
            "-c",
            'source "$1"; orun \'touch "$ORDIN_TEST_SENTINEL"\'',
            "ordin-test",
            str(init_file),
        ],
        text=True,
        capture_output=True,
        env={**env, "ORDIN_TEST_SENTINEL": str(blocked_path)},
        check=False,
    )
    assert result.returncode == exit_code
    assert not blocked_path.exists()
    assert "decision: block" in result.stderr


def test_generated_script_has_valid_syntax(tmp_path, shell):
    name, executable = shell
    init_file = tmp_path / "ordin init.sh"
    init_file.write_text(render_shell_init(name), encoding="utf-8")
    subprocess.run([executable, "-n", str(init_file)], check=True)


def test_wrapper_rejects_bad_threshold_and_can_be_disabled(tmp_path, shell):
    name, executable = shell
    init_file = tmp_path / "ordin init.sh"
    init_file.write_text(render_shell_init(name))
    result = subprocess.run(
        [
            executable,
            "-c",
            'source "$1"; ORDIN_SHELL_FAIL_ON=invalid orun "printf forbidden"; test "$?" = 2 || exit 1; ordin_shell_disable; typeset -f orun && exit 1; exit 0',
            "ordin-test",
            str(init_file),
        ],
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    assert "forbidden" not in result.stdout


@pytest.mark.parametrize("exit_code, accepted", [(0, True), (30, False)])
def test_zsh_widget_obeys_review_result(tmp_path, shell, exit_code, accepted):
    name, executable = shell
    if name != "zsh":
        pytest.skip("ZLE belongs to Zsh")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _fake_ordin(bin_dir / "ordin", decision="allow" if accepted else "block", exit_code=exit_code)
    init_file = tmp_path / "ordin init.zsh"
    init_file.write_text(render_shell_init(name))
    # Exercise the widget in a real Zsh process with a non-executing ZLE boundary.
    script = 'source "$1"; zle() { if [[ "$1" == .accept-line ]]; then print -r -- accepted; fi; }; BUFFER="printf example"; __ordin_zle_review_accept'
    result = subprocess.run(
        [executable, "-c", script, "ordin-test", str(init_file)],
        env={**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"},
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    assert ("accepted" in result.stdout) == accepted
