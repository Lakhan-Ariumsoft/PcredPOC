import pytest
from app.utils.json_tools import (
    parse_json_object,
    clean_numeric_null_fields,
    clean_thousand_separators,
    repair_truncated_json
)
from app.exceptions import JsonParseFailureError


def test_clean_numeric_null_fields():
    raw1 = '"page": 3"\n'
    assert clean_numeric_null_fields(raw1) == '"page": 3\n'

    raw2 = '      "page": 3",\n'
    assert clean_numeric_null_fields(raw2) == '      "page": 3,\n'

    raw3 = '      "value": "123.45",\n'
    assert clean_numeric_null_fields(raw3) == '      "value": 123.45,\n'

    raw4 = '      "confidence": "null",\n'
    assert clean_numeric_null_fields(raw4) == '      "confidence": null,\n'

    raw5 = '      "value": 51.58\n'
    assert clean_numeric_null_fields(raw5) == '      "value": 51.58\n'


def test_repair_truncated_json():
    # Simple brackets
    raw1 = '{"a": 1, "b": {"c": 2'
    assert repair_truncated_json(raw1) == '{"a": 1, "b": {"c": 2}}'

    # Trailing comma
    raw2 = '{"a": 1, "b": {"c": 2,'
    assert repair_truncated_json(raw2) == '{"a": 1, "b": {"c": 2}}'

    # Unclosed string
    raw3 = '{"a": 1, "b": "hello'
    assert repair_truncated_json(raw3) == '{"a": 1, "b": "hello"}'

    # String closed but followed by trailing comma
    raw4 = '{"a": 1, "b": "hello",'
    assert repair_truncated_json(raw4) == '{"a": 1, "b": "hello"}'


def test_clean_thousand_separators_strips_commas_from_unquoted_numbers():
    raw = '"value": 1,112.69,'
    assert clean_thousand_separators(raw) == '"value": 1112.69,'


def test_clean_thousand_separators_strips_commas_from_quoted_numbers():
    raw = '"confidence": "1,234",'
    assert clean_thousand_separators(raw) == '"confidence": "1234",'


def test_clean_thousand_separators_leaves_evidence_text_untouched():
    # "evidence" is not a numeric field — commas inside it are legitimate
    # quoted string content (e.g. table cell text) and must survive as-is.
    raw = '"evidence": "Total | 1,016.97 | 2,189.01",'
    assert clean_thousand_separators(raw) == raw


def test_clean_thousand_separators_handles_negative_numbers():
    raw = '"value": -1,234.56,'
    assert clean_thousand_separators(raw) == '"value": -1234.56,'


def test_parse_json_object_recovers_from_llm_thousands_separator_bug():
    # Reproduces the exact failure observed in production: GPT-4o-mini
    # echoed a comma-formatted number as an unquoted JSON literal, which is
    # invalid JSON grammar and previously made the entire batch fail after
    # exhausting all retries — silently dropping every field in the batch.
    bad_output = """{
  "PAT/Sales": {
    "current": {"value": 0.0342, "confidence": 0.95, "evidence": "Net Profit/(Loss) for the period (3-4) | 1,016.97 | 2,189.01", "page": 14},
    "previous": {"value": 0.0647, "confidence": 0.95, "evidence": "Net Profit/(Loss) for the period (3-4) | 1,016.97 | 2,189.01", "page": 14}
  },
  "Depreciation adjustments": {
    "current": {"value": 1,112.69, "confidence": 0.95, "evidence": "- Depreciation | 1,112.69 | 1,007.33", "page": 15},
    "previous": {"value": 1,007.33, "confidence": 0.95, "evidence": "- Depreciation | 1,112.69 | 1,007.33", "page": 15}
  }
}"""
    parsed = parse_json_object(bad_output)
    assert parsed["Depreciation adjustments"]["current"]["value"] == 1112.69
    assert parsed["Depreciation adjustments"]["previous"]["value"] == 1007.33
    assert parsed["Depreciation adjustments"]["current"]["evidence"] == "- Depreciation | 1,112.69 | 1,007.33"
    assert parsed["PAT/Sales"]["current"]["value"] == 0.0342


def test_parse_json_object_with_truncation_and_bad_quotes():
    # Mix of issues: unbalanced quote in page, trailing commas, and truncated json
    bad_output = """
Some raw conversational text from LLM...
{
  "Domestic Receivables": {
    "current": {
      "value": 2860.76,
      "confidence": 1.0,
      "evidence": "Total | 2,860.76",
      "page": 3"
    },
    "previous": {
      "value": 3116.64,
      "confidence": 1.0,
      "evidence": "Total | 3,116.64",
      "page": 3"
    }
  },
  "Long Term provisions": {
    "current": {
      "value": 51.58,
      "confidence": 1.0,
      "evidence": "Long Term Provisions 6 51.58",
      "page": 15
    },
    "previous": {
      "value": 49.74,
      "confidence": 1.0,
      "evidence": "Long Term Provisions 6 49.74",
      "page": 15
    }
"""
    parsed = parse_json_object(bad_output)
    assert parsed["Domestic Receivables"]["current"]["page"] == 3
    assert parsed["Domestic Receivables"]["previous"]["page"] == 3
    assert parsed["Long Term provisions"]["previous"]["value"] == 49.74
    assert parsed["Long Term provisions"]["previous"]["page"] == 15
