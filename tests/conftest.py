import os
import shutil
import tempfile
import pytest
os.environ.setdefault("PREFECT_HOME", tempfile.mkdtemp(prefix="watcher-test-prefect-"))
os.environ.setdefault("PREFECT_SERVER_ANALYTICS_ENABLED", "false")


@pytest.fixture(scope="session")
def uv_executable():
    executable = os.environ.get("WATCHER_UV") or shutil.which("uv")
    if not executable:
        pytest.fail("Install uv or set WATCHER_UV to its executable before running integration/native tests")
    return executable


@pytest.fixture(autouse=True)
def isolated_runtime(tmp_path, monkeypatch):
    # Subprocesses inherit this too: synthetic data must not enter the app's
    # real, Git-ignored runtime directory or another test's workspace identity.
    runtime = tmp_path / "local-runtime"
    monkeypatch.setenv("WATCHER_RUNTIME_HOME", str(runtime))
    return runtime
