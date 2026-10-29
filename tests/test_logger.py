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


# Fake keys are assembled at runtime so no key-shaped literal is committed
_FAKE_ANTHROPIC = "sk-" + "ant-api03-abcDEF123456789xyzABC"
_FAKE_GOOGLE = "AI" + "zaSyA1234567890abcdefghijklmnopqr"
_FAKE_NOTION = "ntn" + "_1234567890abcdefghijABCDEFG"


class TestRedaction:
    @pytest.mark.parametrize("text", [
        "job 3f2a" + "b" * 60,                         # SHA-256 job ID
        "app 812464b9-1c44-48b7-8fed-3dda9770a1c3",    # UUID
        "scraped 2026-04-13 at 13:40",                 # date and time
        "score=7.5 batch=50",
    ])
    def test_identifiers_and_dates_kept(self, text):
        assert logger_module._redact(text) == text

    @pytest.mark.parametrize("secret", [
        _FAKE_ANTHROPIC,
        "gsk_abcdefghijklmnopqrstuvwxyz123",
        _FAKE_GOOGLE,
        _FAKE_NOTION,
        "Xy7aB9cD2eF4gH6iJ8kL0mN1oP3qR5sT7uV",
    ])
    def test_secrets_redacted(self, secret):
        assert secret not in logger_module._redact(f"value {secret} end")

    def test_url_key_param_redacted(self):
        url = "https://api.adzuna.com/x?app_id=1&app_key=0123456789abcdef0123456789abcdef"
        assert "0123456789abcdef" not in logger_module._redact(url)

    @pytest.mark.parametrize("phone", ["+49 911 1234567", "+1 (415) 555-0100", "0911 1234567"])
    def test_phone_numbers_redacted(self, phone):
        assert logger_module._redact(f"call {phone}") == "call [PHONE]"

    def test_email_redacted(self):
        assert logger_module._redact("from jane.doe@example.com") == "from [EMAIL]"

    def test_traceback_and_utc_timestamp(self, tmp_path):
        logger_module.setup(logs_dir=tmp_path)
        log = get_logger("t")
        try:
            raise RuntimeError(f"bad key {_FAKE_ANTHROPIC}")
        except RuntimeError:
            log.exception("failed")
        for h in _handlers():
            h.flush()
        content = (tmp_path / "applai.log").read_text()
        assert _FAKE_ANTHROPIC not in content and "[SECRET]" in content
        assert content.split(" | ")[0].endswith("Z")
