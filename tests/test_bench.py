import glob
import importlib.util
import os
import re
import sys
import unittest

from helpers import ROOT

_spec = importlib.util.spec_from_file_location("bench", os.path.join(ROOT, "bench.py"))
bench = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bench)


class SearchBudget(unittest.TestCase):
    """CLAUDE.md budget: FTS results < 50 ms on 2,000 notes."""

    def test_2000_notes_under_50ms(self):
        # its cold-CLI runs use the launcher: no byte-code cache in the real ~/.cache
        os.environ["PYTHONDONTWRITEBYTECODE"], old = "1", os.environ.get("PYTHONDONTWRITEBYTECODE")
        try:
            r = bench.run(2000, reps=3, with_semantic=False)
        finally:
            if old is None:
                os.environ.pop("PYTHONDONTWRITEBYTECODE")
            else:
                os.environ["PYTHONDONTWRITEBYTECODE"] = old
        sys.stderr.write("\n[bench] " + " | ".join(
            f"{k} median {v['median_ms']} p95 {v['p95_ms']} max {v['max_ms']} ms"
            for k, v in r.items() if isinstance(v, dict)) + "\n")
        self.assertLess(r["search_inprocess"]["p95_ms"], 50)
        self.assertLess(r["search_serve_roundtrip"]["p95_ms"], 50)



class QmlBenchHarnessTest(unittest.TestCase):
    """Each QML bench runs the plugin in its own `quickshell -p` dir and
    links in the system shell's modules; one missing (as bench_shell.py
    once missed qs.Ui) only shows up as 'no result from harness'."""

    def test_benches_link_every_qs_module_the_plugin_imports(self):
        used = set()
        for path in glob.glob(os.path.join(ROOT, "*.qml")):
            with open(path) as f:
                used |= set(re.findall(r"^import qs\.(\w+)", f.read(), re.M))
        self.assertTrue(used)
        for bench_py in ("bench_shell.py", "bench_bar.py", "bench_search.py", "bench_chat.py", "bench_list.py"):
            with open(os.path.join(ROOT, bench_py)) as f:
                src = f.read()
            linked = set(re.findall(r'for name in \(([^)]*)\)', src))
            names = {n for group in linked for n in re.findall(r'"(\w+)"', group)}
            self.assertLessEqual(used, names, bench_py)


if __name__ == "__main__":
    unittest.main()
