from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest

from cvboost import cli
from cvboost.locks import LockBusy, holder_info, pipeline_lock, try_lock


def test_second_lock_fails_until_released(tmp_path):
    p = tmp_path / "x.lock"
    a = try_lock(p)
    assert a is not None and try_lock(p) is None
    assert "pid" in holder_info(p)  # the failed attempt did not wipe the holder's info
    a.release()
    b = try_lock(p)
    assert b is not None
    b.release()


def test_lock_across_processes(tmp_path):
    p = tmp_path / "x.lock"
    code = textwrap.dedent(
        f"""
        import sys, time
        from pathlib import Path
        from cvboost.locks import try_lock
        h = try_lock(Path({str(p)!r}))
        print("locked" if h else "busy", flush=True)
        sys.stdin.readline()
        """
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", code], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True
    )
    try:
        assert proc.stdout.readline().strip() == "locked"
        assert try_lock(p) is None
        assert str(proc.pid) in holder_info(p)
    finally:
        proc.stdin.write("\n")
        proc.stdin.flush()
        proc.wait(timeout=10)
    h = try_lock(p)  # the kernel released it when the process exited
    assert h is not None
    h.release()


def test_lock_dies_with_process_even_if_killed(tmp_path):
    p = tmp_path / "x.lock"
    code = (
        "import time; from pathlib import Path; from cvboost.locks import try_lock; "
        f"h = try_lock(Path({str(p)!r})); print('ok', flush=True); time.sleep(60)"
    )
    proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    assert proc.stdout.readline().strip() == "ok"
    proc.kill()
    proc.wait()
    h = try_lock(p)
    assert h is not None
    h.release()


def test_pipeline_lock_waits_then_raises(tmp_path):
    held = try_lock(tmp_path / "pipeline.lock")
    t = [0.0]
    sleeps = []

    def sleep(s):
        sleeps.append(s)
        t[0] += s

    with (
        pytest.raises(LockBusy),
        pipeline_lock(tmp_path, wait_s=2, sleep=sleep, clock=lambda: t[0]),
    ):
        pass
    assert sleeps == [0.5] * 4
    held.release()
    with pipeline_lock(tmp_path, wait_s=0):
        pass


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("CVBOOST_DB", str(tmp_path / "cli.sqlite"))
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    return tmp_path


def test_second_worker_exits_with_message(env, capsys):
    held = try_lock(env / "worker.lock")
    rc = cli.main(["worker", "--once"])
    err = capsys.readouterr().err
    assert rc == 3 and "another worker is running" in err and "pid" in err
    held.release()


def test_cli_collect_triage_score_refuse_while_pipeline_busy(env, capsys):
    held = try_lock(env / "pipeline.lock")
    for cmd in (["collect"], ["triage"], ["score"]):
        assert cli.main(cmd) == 3
        assert "pipeline busy" in capsys.readouterr().err
    assert cli.main(["score", "--dry-run"]) == 0  # read-only
    assert cli.main(["sources"]) == 0
    held.release()
    assert cli.main(["triage"]) == 0


def test_bad_chat_id_exits_2(env, monkeypatch, capsys):
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "not-a-number")
    assert cli.main(["sources"]) == 2
    assert "TELEGRAM_CHAT_ID" in capsys.readouterr().err
