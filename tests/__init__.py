"""ARX Insight test suite (stdlib unittest, synthetic data only - never a real ARX database).

Run from the project root:   ./.venv/bin/python -m unittest discover -s tests -t . -v

Importing this package first points ARX_DATA_DIR at a throw-away folder, so that neither the
settings nor the caches of a real installation are ever touched by a test (arx_report / arx_app
compute their file locations at import time).
"""
import atexit, os, shutil, tempfile

_DATA = tempfile.mkdtemp(prefix="arx_test_data_")
os.environ["ARX_DATA_DIR"] = _DATA
os.environ["ARX_NO_UPDATE_CHECK"] = "1"          # tests never go online
atexit.register(shutil.rmtree, _DATA, ignore_errors=True)
