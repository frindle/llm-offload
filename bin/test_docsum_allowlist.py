"""docsum_run.py is an allowlisted queue runner (exact path, resolved form too)."""
import importlib.util
from pathlib import Path

HERE = Path(__file__).resolve().parent
_sp = importlib.util.spec_from_file_location("oq_docsum", HERE / "ollama-queue.py")
Q = importlib.util.module_from_spec(_sp)
_sp.loader.exec_module(Q)


def test_docsum_runner_allowlisted():
    p = Path.home() / "bin" / "docsum_run.py"
    assert str(p) in Q.ALLOWED_RUNNERS
    assert str(p.resolve()) in Q.ALLOWED_RUNNERS
    assert p.is_file()


def test_allowlist_is_still_exact_paths():
    assert str(Path.home() / "bin" / "docsum_run_evil.py") not in Q.ALLOWED_RUNNERS
    assert str(Path.home() / "bin" / "studio-research.py") in Q.ALLOWED_RUNNERS
