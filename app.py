import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "backend"))
import importlib.util
spec = importlib.util.spec_from_file_location(
    "index833",
    os.path.join(os.path.dirname(__file__), "backend", "indexv8_3_3.py")
)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
app = mod.app
