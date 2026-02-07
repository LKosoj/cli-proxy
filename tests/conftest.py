import os
import sys


# Allow importing top-level modules (bot.py, config.py, etc.) from tests/.
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
