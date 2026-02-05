from __future__ import annotations

import importlib.util
import inspect
import logging
from pathlib import Path
from typing import List

from agent.plugins.base import ToolPlugin


class PluginLoader:
    def __init__(self, plugins_directory: Path) -> None:
        self.plugins_directory = plugins_directory

    def load(self) -> List[ToolPlugin]:
        plugins: List[ToolPlugin] = []
        try:
            plugins_path = Path(self.plugins_directory)
            excluded = {"__init__.py", "base.py"}
            for plugin_file in sorted(plugins_path.glob("*.py")):
                if plugin_file.name in excluded:
                    continue
                module = self._load_module(plugin_file)
                if not module:
                    continue
                classes = [
                    cls
                    for _, cls in inspect.getmembers(module, inspect.isclass)
                    if issubclass(cls, ToolPlugin) and cls is not ToolPlugin
                ]
                if not classes:
                    logging.warning(f"No plugin class found in {plugin_file.name}")
                    continue
                for cls in classes:
                    try:
                        plugins.append(cls())
                    except Exception as e:
                        logging.exception(f"tool failed {str(e)}")
                        continue
        except Exception as e:
            logging.exception(f"tool failed {str(e)}")
        return plugins

    def _load_module(self, path: Path):
        try:
            spec = importlib.util.spec_from_file_location(f"agent.plugins.{path.stem}", path)
            if not spec or not spec.loader:
                return None
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
        except Exception as e:
            logging.exception(f"tool failed {str(e)}")
            return None
