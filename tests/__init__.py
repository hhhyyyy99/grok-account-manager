import atexit
import os
import tempfile


_TEST_DATA_DIRECTORY = tempfile.TemporaryDirectory(prefix="grok-manager-tests-")
atexit.register(_TEST_DATA_DIRECTORY.cleanup)
os.environ["GROK_MANAGER_DATA_DIR"] = _TEST_DATA_DIRECTORY.name
os.environ["GROK_MANAGER_CONFIG"] = os.path.join(
    _TEST_DATA_DIRECTORY.name,
    "config.json",
)
