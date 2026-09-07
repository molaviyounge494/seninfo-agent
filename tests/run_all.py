"""Zero-dependency test runner (pytest unavailable offline).

Usage: PYTHONPATH=src:tests python3 tests/run_all.py
When pytest is available, `pytest tests/` covers the same functions.
"""

import importlib
import sys
import traceback

MODULES = ["test_core", "test_phase1", "e2e_acc"]


def main() -> int:
    passed = failed = 0
    failures = []
    for mod_name in MODULES:
        mod = importlib.import_module(mod_name)
        for name in sorted(n for n in dir(mod) if n.startswith("test_")):
            try:
                getattr(mod, name)()
                passed += 1
            except Exception:
                failed += 1
                failures.append("%s.%s" % (mod_name, name))
                print("FAIL %s.%s" % (mod_name, name))
                traceback.print_exc()
    print("----")
    print("%d passed, %d failed" % (passed, failed))
    if failures:
        print("failed: %s" % ", ".join(failures))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
