"""
Companest Configuration Watcher

Provides hot-reload capability for Companest configuration files.
Monitors configuration files for changes and automatically reloads.

Features:
- File modification detection
- Debounced reloading (avoid rapid reloads)
- Callback-based notification
- Thread-safe operation
- Graceful degradation if watchdog not available

Usage:
    from companest.watcher import ConfigWatcher

    def on_reload(new_config):
        print(f"Config reloaded: {new_config.name}")

    watcher = ConfigWatcher(".companest/config.md", on_reload=on_reload)
    watcher.start()

    # ... your application runs ...

    watcher.stop()
"""

import os
import time
import logging
import threading
from typing import Callable, List, Optional, Union, Any
from pathlib import Path
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib

from .config import CompanestConfig
from .parser import MarkdownConfigParser
from .exceptions import ConfigurationError

logger = logging.getLogger(__name__)


@dataclass
class ConfigChangeEvent:
    """
    Represents a configuration change event.

    Attributes:
        path: Path to the changed file
        old_config: Previous configuration (None on first load)
        new_config: New configuration
        timestamp: When the change was detected
        change_type: Type of change ('created', 'modified', 'deleted')
    """
    path: Path
    old_config: Optional[CompanestConfig]
    new_config: Optional[CompanestConfig]
    timestamp: datetime
    change_type: str

    @property
    def is_valid(self) -> bool:
        """Check if new config is valid"""
        return self.new_config is not None


# Type for reload callback
ReloadCallback = Callable[[ConfigChangeEvent], None]


class ConfigWatcher:
    """
    Watches configuration files for changes and triggers reloads.

    Uses file modification time and content hash to detect changes.
    Supports both polling mode (built-in) and event-based mode (with watchdog).

    Example:
        watcher = ConfigWatcher(".companest/config.md")
        watcher.on_reload(lambda event: print(f"Reloaded: {event.new_config}"))
        watcher.start()

        config = watcher.current_config

        watcher.stop()
    """

    def __init__(
        self,
        config_path: Union[str, Path],
        on_reload: Optional[ReloadCallback] = None,
        debounce_seconds: float = 1.0,
        poll_interval: float = 2.0,
        auto_start: bool = False,
        validate_on_reload: bool = True
    ):
        self.config_path = Path(config_path).resolve()
        self.debounce_seconds = debounce_seconds
        self.poll_interval = poll_interval
        self.validate_on_reload = validate_on_reload

        self._callbacks: List[ReloadCallback] = []
        if on_reload:
            self._callbacks.append(on_reload)

        self._current_config: Optional[CompanestConfig] = None
        self._last_modified: float = 0
        self._last_hash: str = ""
        self._last_reload: float = 0

        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.RLock()

        self._parser = MarkdownConfigParser()

        self._load_initial_config()

        if auto_start:
            self.start()

    def _load_initial_config(self):
        """Load the initial configuration"""
        try:
            if self.config_path.exists():
                self._current_config = self._load_config()
                self._last_modified = self.config_path.stat().st_mtime
                self._last_hash = self._compute_hash()
                logger.info(f"Loaded initial config from {self.config_path}")
        except Exception as e:
            logger.warning(f"Failed to load initial config: {e}")

    @property
    def current_config(self) -> Optional[CompanestConfig]:
        """Get the current configuration (thread-safe)"""
        with self._lock:
            return self._current_config

    @property
    def is_running(self) -> bool:
        """Check if watcher is running"""
        return self._running

    def on_reload(self, callback: ReloadCallback) -> "ConfigWatcher":
        """Register a reload callback."""
        self._callbacks.append(callback)
        return self

    def start(self) -> "ConfigWatcher":
        """Start watching for configuration changes."""
        if self._running:
            logger.warning("ConfigWatcher already running")
            return self

        self._running = True

        if self._try_start_watchdog():
            logger.info(f"Started watching {self.config_path} (watchdog mode)")
        else:
            self._thread = threading.Thread(
                target=self._poll_loop,
                name="ConfigWatcher-Poll",
                daemon=True
            )
            self._thread.start()
            logger.info(f"Started watching {self.config_path} (polling mode)")

        return self

    def stop(self):
        """Stop watching for changes"""
        self._running = False
        if self._thread:
            self._thread.join(timeout=self.poll_interval + 1)
            self._thread = None

        if hasattr(self, "_observer"):
            self._observer.stop()
            self._observer.join()

        logger.info("ConfigWatcher stopped")

    def _try_start_watchdog(self) -> bool:
        """Try to start watchdog-based watching"""
        try:
            from watchdog.observers import Observer
            from watchdog.events import FileSystemEventHandler

            class Handler(FileSystemEventHandler):
                def __init__(handler_self, watcher: "ConfigWatcher"):
                    handler_self.watcher = watcher

                def on_modified(handler_self, event):
                    if event.is_directory:
                        return
                    if Path(event.src_path).resolve() == handler_self.watcher.config_path:
                        handler_self.watcher._on_file_changed()

            self._observer = Observer()
            self._observer.schedule(
                Handler(self),
                str(self.config_path.parent),
                recursive=False
            )
            self._observer.start()
            return True

        except ImportError:
            logger.debug("watchdog not available, using polling mode")
            return False

    def _poll_loop(self):
        """Polling loop for detecting changes"""
        while self._running:
            try:
                if self._check_for_changes():
                    self._on_file_changed()
            except Exception as e:
                logger.error(f"Error in poll loop: {e}")

            time.sleep(self.poll_interval)

    def _check_for_changes(self) -> bool:
        """Check if the config file has changed"""
        if not self.config_path.exists():
            return False

        try:
            current_mtime = self.config_path.stat().st_mtime
            if current_mtime != self._last_modified:
                current_hash = self._compute_hash()
                if current_hash != self._last_hash:
                    return True
        except Exception as e:
            logger.debug(f"Error checking file: {e}")

        return False

    def _compute_hash(self) -> str:
        """Compute hash of config file content"""
        try:
            content = self.config_path.read_bytes()
            return hashlib.md5(content).hexdigest()
        except Exception:
            return ""

    def _on_file_changed(self):
        """Handle file change event"""
        now = time.time()
        if now - self._last_reload < self.debounce_seconds:
            return

        with self._lock:
            old_config = self._current_config

            try:
                new_config = self._load_config()

                if self.validate_on_reload:
                    errors = self._parser.validate_config(new_config.to_dict())
                    if errors:
                        logger.warning(f"New config validation failed: {errors}")
                        return

                self._current_config = new_config
                self._last_modified = self.config_path.stat().st_mtime
                self._last_hash = self._compute_hash()
                self._last_reload = now

                event = ConfigChangeEvent(
                    path=self.config_path,
                    old_config=old_config,
                    new_config=new_config,
                    timestamp=datetime.now(timezone.utc),
                    change_type="modified"
                )

                self._notify_callbacks(event)

                logger.info(f"Configuration reloaded from {self.config_path}")

            except Exception as e:
                logger.error(f"Failed to reload config: {e}")

    def _load_config(self) -> CompanestConfig:
        """Load configuration from file"""
        result = self._parser.parse_file(self.config_path)
        return CompanestConfig(**result.config)

    def _notify_callbacks(self, event: ConfigChangeEvent):
        """Notify all registered callbacks"""
        for callback in self._callbacks:
            try:
                callback(event)
            except Exception as e:
                logger.error(f"Error in reload callback: {e}")

    def reload(self) -> bool:
        """
        Manually trigger a reload.

        Returns:
            True if reload succeeded, False otherwise
        """
        try:
            self._on_file_changed()
            return True
        except Exception as e:
            logger.error(f"Manual reload failed: {e}")
            return False

    def __enter__(self) -> "ConfigWatcher":
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()


class HotReloadOrchestrator:
    """
    Companest Orchestrator wrapper with hot-reload support.

    Automatically updates the orchestrator when configuration changes.

    Example:
        from companest.watcher import HotReloadOrchestrator

        orchestrator = HotReloadOrchestrator(".companest/config.md")

        result = await orchestrator.run_team("My task", "team-id")
    """

    def __init__(
        self,
        config_path: Union[str, Path],
        on_config_change: Optional[Callable[[CompanestConfig], None]] = None
    ):
        from .orchestrator import CompanestOrchestrator

        self._on_config_change = on_config_change
        # threading lock for _handle_reload (called from watcher thread)
        self._reload_lock = threading.RLock()

        self._watcher = ConfigWatcher(
            config_path,
            on_reload=self._handle_reload,
            auto_start=True
        )

        config = self._watcher.current_config
        if config:
            self._orchestrator = CompanestOrchestrator(config)
        else:
            raise ConfigurationError(f"Failed to load config from {config_path}")

    def _handle_reload(self, event: ConfigChangeEvent):
        """Handle config reload event (called from watcher thread)."""
        if not event.is_valid:
            return

        from .orchestrator import CompanestOrchestrator

        with self._reload_lock:
            logger.info("Applying new configuration to orchestrator")
            # Atomic reference swap  safe to read from asyncio without lock
            self._orchestrator = CompanestOrchestrator(event.new_config)

            if self._on_config_change:
                try:
                    self._on_config_change(event.new_config)
                except Exception as e:
                    logger.error(f"Error in config change callback: {e}")

    @property
    def config(self) -> Optional[CompanestConfig]:
        """Get current configuration"""
        return self._watcher.current_config

    @property
    def orchestrator(self):
        """Get current orchestrator instance (atomic read, no lock needed)."""
        return self._orchestrator

    async def run_team(self, task: str, team_id: str, **kwargs) -> str:
        """Run a task on a team through the orchestrator.

        No lock held across await  the orchestrator reference is swapped
        atomically by _handle_reload under the GIL.
        """
        orch = self._orchestrator  # snapshot reference
        return await orch.run_team(task, team_id, **kwargs)

    def stop(self):
        """Stop the watcher"""
        self._watcher.stop()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()
