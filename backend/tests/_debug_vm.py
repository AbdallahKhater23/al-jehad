"""Temporary diagnostic. Deleted after use."""

import importlib.util
import json
import sys

sys.path.insert(0, ".")

import frontend_vm  # noqa: E402

HERE = __file__.rsplit("\\", 1)[0].rsplit("/", 1)[0] + "/"
spec = importlib.util.spec_from_file_location("t", HERE + "test_frontend_shift_rules.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

harness = module.HARNESS.replace(
    "results.panel = {",
    "results.probe = JSON.stringify({override: activeOverride, body: stats(activeOverride)});\n    results.raw_panel = markup;\n    results.panel = {",
)
results = frontend_vm.run(harness)
print(json.dumps({k: results[k] for k in ("mid_day",)}, indent=2))
print(results["probe"][:400])
