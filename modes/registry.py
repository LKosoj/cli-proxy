from __future__ import annotations

import importlib.util
import inspect
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from modes.sdk import BaseMode


@dataclass
class ModeRegistry:
    """In-memory store of loaded mode plugins."""

    modes: Dict[str, BaseMode] = field(default_factory=dict)
    log: logging.Logger = field(default_factory=lambda: logging.getLogger(__name__))

    def register(self, mode: BaseMode) -> None:
        mode_id = mode.get_mode_id()
        if not mode_id:
            raise ValueError("Mode id is empty")
        if mode_id in self.modes:
            raise ValueError(f"Duplicate mode id: {mode_id}")
        self.modes[mode_id] = mode

    def get(self, mode_id: str) -> Optional[BaseMode]:
        return self.modes.get(str(mode_id))

    def list_ids(self) -> List[str]:
        return sorted(self.modes.keys())


class ModeLoader:
    """
    Discover and load mode plugins.

    Scans the `modes/` directory for subdirectories that contain `__init__.py`
    and export `PLUGIN`.
    """

    def __init__(
        self,
        modes_dir: Optional[Path] = None,
        *,
        module_prefix: str = "modes.dynamic",
        excluded_dirs: Optional[set[str]] = None,
    ) -> None:
        self.modes_dir = Path(modes_dir or (Path(__file__).resolve().parent))
        self.module_prefix = str(module_prefix)
        self.excluded_dirs = excluded_dirs or {"sdk", "__pycache__"}
        self._log = logging.getLogger(__name__)

    def discover(self) -> List[Tuple[str, Path]]:
        """Return list of (mode_name, init_py_path) discovered under modes_dir."""
        found: List[Tuple[str, Path]] = []
        if not self.modes_dir.exists():
            return []
        for p in sorted(self.modes_dir.iterdir()):
            if not p.is_dir():
                continue
            if p.name in self.excluded_dirs:
                continue
            init_py = p / "__init__.py"
            if not init_py.exists():
                continue
            found.append((p.name, init_py))
        return found

    def load_all(self) -> List[BaseMode]:
        plugins: List[BaseMode] = []
        for name, init_py in self.discover():
            mode = self._load_one(name, init_py)
            if mode is None:
                continue
            plugins.append(mode)
        return plugins

    def load_into(self, registry: ModeRegistry) -> int:
        """Load all modes and register them. Returns number of registered modes."""
        loaded = 0
        for mode in self.load_all():
            try:
                registry.register(mode)
                loaded += 1
            except Exception as e:
                self._log.exception("mode register failed mode=%s err=%s", getattr(mode, "mode_id", ""), e)
                continue
        return loaded

    def _load_one(self, name: str, init_py: Path) -> Optional[BaseMode]:
        try:
            module = self._load_package(name, init_py)
            if not module:
                return None
            if not hasattr(module, "PLUGIN"):
                self._log.warning("mode skipped (no PLUGIN export): %s", name)
                return None
            plugin = getattr(module, "PLUGIN")
            mode = self._coerce_plugin(plugin, name=name)
            if mode is None:
                self._log.warning("mode skipped (invalid PLUGIN type): %s", name)
                return None
            return mode
        except Exception as e:
            # One broken plugin must not break the entire loader.
            self._log.exception("mode load failed: %s err=%s", name, e)
            return None

    def _coerce_plugin(self, plugin, *, name: str) -> Optional[BaseMode]:
        # PLUGIN may be a BaseMode instance.
        if isinstance(plugin, BaseMode):
            return plugin
        # Or a BaseMode subclass.
        if inspect.isclass(plugin) and issubclass(plugin, BaseMode):
            try:
                return plugin()
            except Exception as e:
                self._log.exception("mode instantiate failed: %s err=%s", name, e)
                return None
        return None

    def _load_package(self, name: str, init_py: Path):
        package_dir = init_py.parent
        module_name = f"{self.module_prefix}.{name}"
        try:
            spec = importlib.util.spec_from_file_location(
                module_name,
                init_py,
                submodule_search_locations=[str(package_dir)],
            )
            if not spec or not spec.loader:
                return None
            module = importlib.util.module_from_spec(spec)
            # Some decorators (e.g. dataclasses) expect the module to be present in sys.modules
            # while the module code is executed.
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
            return module
        except Exception as e:
            self._log.exception("mode import failed: %s err=%s", name, e)
            return None
