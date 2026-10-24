"""Tests for logger setup (src/logger.py)."""
import logging
from logging.handlers import RotatingFileHandler

import pytest

import src.logger as logger_module
from src.logger import audit, get_logger


@pytest.fixture(autouse=True)
def reset_logging():
    """Restore console-only logging after each test so other tests are unaffected."""
    yield
    logger_module.setup()


def _handlers():
    return logging.getLogger("applai").handlers


class TestSetup:
    def test_setup_after_import_enables_file_logging(self, tmp_path):
        # Modules call get_logger() at import time, before the entrypoint's setup()
        log = get_logger("some.module")
        logger_module.setup(logs_dir=tmp_path, debug=False)

        log.info("hello file")
        audit("test_event", job="j1")
        for h in _handlers():
            h.flush()

        content = (tmp_path / "applai.log").read_text()
        assert "hello file" in content
        assert "[AUDIT] action=test_event" in content

    def test_debug_flag_applies_to_console(self, tmp_path):
        get_logger("x")
        logger_module.setup(logs_dir=tmp_path, debug=True)
        console = next(h for h in _handlers() if not isinstance(h, RotatingFileHandler))
        assert console.level == logging.DEBUG

    def test_repeated_setup_does_not_duplicate_handlers(self, tmp_path):
        logger_module.setup(logs_dir=tmp_path)
        logger_module.setup(logs_dir=tmp_path)
        assert len(_handlers()) == 2  # one console + one file

    def test_get_logger_without_setup_has_console(self):
        logging.getLogger("applai").handlers.clear()
        get_logger("y")
        assert len(_handlers()) == 1
