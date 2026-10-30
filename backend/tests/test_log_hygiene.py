"""Session ids (wa-<phone>-<n>) and phone numbers must never reach the logs raw."""
import ast
import logging
import pathlib

from app import session_store
from app.config import settings
from app.main import _OneLineRecord
from app.session_store import mask_session_id

APP = pathlib.Path(__file__).resolve().parent.parent / "app"
LOG_METHODS = {"debug", "info", "warning", "error", "exception", "critical"}
SENSITIVE = {"session_id", "phone", "key", "authToken"}


def test_hash_hides_the_phone_and_is_stable():
    sid = "wa-919876543210-1"
    out = mask_session_id(sid)
    assert out.startswith("sid-") and "9198" not in out and "3210" not in out
    assert out == mask_session_id(sid) and out != mask_session_id("wa-919876543211-1")


def test_hash_depends_on_the_secret_key(monkeypatch):
    monkeypatch.setattr(settings, "log_hash_key", "key-one")
    a = mask_session_id("wa-919876543210-1")
    monkeypatch.setattr(settings, "log_hash_key", "key-two")
    assert a != mask_session_id("wa-919876543210-1")   # not reversible by hashing every phone number alone


def test_line_breaks_in_a_client_session_id_cannot_forge_log_lines():
    assert "\n" not in mask_session_id("x\nINFO | forged line")
    rec = _OneLineRecord("t", logging.INFO, __file__, 1, "bad %s", ("a\r\nb",), None)
    assert "\n" not in rec.getMessage() and "\r" not in rec.getMessage()


def _leaks(node):
    """Names like session_id/phone used inside a logger call without mask_session_id()."""
    if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "mask_session_id":
        return []
    found = []
    if isinstance(node, ast.Name) and node.id in SENSITIVE:
        found.append(node.id)
    if isinstance(node, ast.Attribute) and node.attr in SENSITIVE:
        found.append(node.attr)
    if isinstance(node, ast.Constant) and node.value == "session_id":     # record.get("session_id")
        found.append("session_id")
    for child in ast.iter_child_nodes(node):
        found += _leaks(child)
    return found


def test_no_logger_call_in_app_uses_a_raw_session_id_or_phone():
    bad = []
    for path in APP.glob("[!.]*.py"):   # skips macOS ._*.py resource-fork files
        for node in ast.walk(ast.parse(path.read_text())):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr in LOG_METHODS and getattr(node.func.value, "id", "") == "logger"):
                for arg in node.args[1:] if node.args and isinstance(node.args[0], ast.Constant) else node.args:
                    if _leaks(arg):
                        bad.append(f"{path.name}:{node.lineno}")
    assert not bad, f"raw ids/phones in log calls (wrap in mask_session_id): {sorted(set(bad))}"
