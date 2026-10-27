"""Tests for src/matching/response_parser.py — scoring reply parsing."""
import pytest

from src.matching.response_parser import ScoreParseError, parse_score_response

_VALID = '{"score": 7.5, "reasoning": "Good fit", "must_haves_met": ["Python"], "gaps": ["Go"]}'


class TestAcceptedShapes:
    def test_plain_json(self):
        result = parse_score_response(_VALID)
        assert result.score == 7.5
        assert result.reasoning == "Good fit"
        assert result.must_haves_met == ["Python"]
        assert result.gaps == ["Go"]

    def test_markdown_fenced(self):
        assert parse_score_response(f"```json\n{_VALID}\n```").score == 7.5

    def test_json_wrapped_in_prose(self):
        assert parse_score_response(f"Here is my assessment:\n{_VALID}\nHope this helps!").score == 7.5

    def test_think_block_stripped(self):
        reply = '<think>The score field says {"score": 1} hmm</think>\n' + _VALID
        assert parse_score_response(reply).score == 7.5

    def test_truncated_json_recovers_explicit_score(self):
        result = parse_score_response('{"score": 6.0, "reasoning": "Strong ML backgr')
        assert result.score == 6.0
        assert "recovered" in result.reasoning

    def test_score_as_numeric_string(self):
        assert parse_score_response('{"score": "8", "reasoning": "x"}').score == 8.0

    @pytest.mark.parametrize("value,expected", [
        ("true", True), ("false", False), ('"false"', False), ('"no"', True), (None, True),
    ])
    def test_is_job_posting_defaults_to_true(self, value, expected):
        field = "" if value is None else f', "is_job_posting": {value}'
        assert parse_score_response(f'{{"score": 5{field}}}').is_job_posting is expected

    def test_list_fields_tolerate_strings(self):
        result = parse_score_response('{"score": 5, "must_haves_met": "Python", "gaps": null}')
        assert result.must_haves_met == ["Python"]
        assert result.gaps == []


class TestRejectedShapes:
    @pytest.mark.parametrize("raw", [None, "", "   "])
    def test_empty_reply(self, raw):
        with pytest.raises(ScoreParseError):
            parse_score_response(raw)

    def test_prose_without_score(self):
        with pytest.raises(ScoreParseError):
            parse_score_response("Strong fit overall, I'd say 8/10")

    @pytest.mark.parametrize("raw", [
        '{"score": "8/10"}',
        '{"score": null}',
        '{"reasoning": "no score key"}',
        '{"score": 15}',
        '{"score": -1}',
        '{"score": NaN}',
    ])
    def test_invalid_score(self, raw):
        with pytest.raises(ScoreParseError):
            parse_score_response(raw)
