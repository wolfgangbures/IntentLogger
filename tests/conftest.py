"""Load dependency-free components without importing Home Assistant on Windows."""

import sys
from pathlib import Path
from types import ModuleType

package = ModuleType("intent_logger_units")
package.__path__ = [
    str(Path(__file__).parents[1] / "custom_components" / "intent_logger")
]
sys.modules[package.__name__] = package
