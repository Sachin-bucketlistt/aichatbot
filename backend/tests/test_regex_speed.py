"""Every compiled regex in app/ must run in linear time on hostile input (no ReDoS)."""
import importlib
import pathlib
import re
import time

from app.region import split_region_tag

APP = pathlib.Path(__file__).resolve().parent.parent / "app"
N = 40_000          # a quadratic pattern takes seconds here, a linear one a few milliseconds
BUDGET = 0.2        # seconds per search


def _collect(name, obj, found, depth=0):
    if isinstance(obj, re.Pattern):
        found[name] = obj
    elif isinstance(obj, (list, tuple)) and depth < 3:     # e.g. handoff._BACKSTOP = [(reason, pattern)]
        for i, item in enumerate(obj):
            _collect(f"{name}[{i}]", item, found, depth + 1)


def _all_patterns():
    found = {}
    for path in sorted(APP.glob("[!._]*.py")):               # skips macOS ._*.py resource forks
        module = importlib.import_module(f"app.{path.stem}")
        for key, value in vars(module).items():
            if not key.startswith("__"):
                _collect(f"{path.stem}.{key}", value, found)
    return found


def _hostile():
    return {
        "spaces": " " * N, "a": "a" * N, "digits": "1" * N, "tilde": "~" * N, "lt": "<" * N, "paren": "(" * N,
        "dash": "-" * N, "digit-space": "1 " * (N // 2), "space-digit": " 1" * (N // 2), "word-space": "x " * (N // 2),
        "rs": "rs " * (N // 3), "tilde-digit": "~1" * (N // 2), "call": "call " * (N // 5),
        "discount-pad": "discount" + " " * N, "almost-id": "a" * (N - 1) + "!",
    }


def test_every_regex_in_app_is_linear_on_hostile_input():
    patterns, slow = _all_patterns(), []
    assert len(patterns) >= 15                                # the collector really found them
    for pname, pattern in patterns.items():
        for iname, text in _hostile().items():
            start = time.perf_counter()
            pattern.search(text)
            took = time.perf_counter() - start
            if took > BUDGET:
                slow.append(f"{pname} on {iname}: {took:.2f}s")
    assert not slow, slow


def test_region_tag_search_is_fast_on_a_wall_of_spaces():
    start = time.perf_counter()
    assert split_region_tag(" " * 100_000 + "(GJ)") == ("", "GJ")
    assert time.perf_counter() - start < 0.5
