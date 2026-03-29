"""First-party plugins for PyJS."""
from .storage import StoragePlugin
from .fetch import FetchPlugin
from .events import EventEmitterPlugin
from .fs import FileSystemPlugin
from .console_ext import ConsoleExtPlugin

__all__ = ['StoragePlugin', 'FetchPlugin', 'EventEmitterPlugin', 'FileSystemPlugin', 'ConsoleExtPlugin']
