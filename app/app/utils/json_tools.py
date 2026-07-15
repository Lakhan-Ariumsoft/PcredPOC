import json
import re
from typing import Any

from app.exceptions import JsonParseFailureError


def clean_unescaped_quotes(raw_json: str) -> str:
    def replace_line(match):
        indent = match.group(1)
        key = match.group(2)
        val = match.group(3)
        comma = match.group(4)
        
        cleaned_val = []
        i = 0
        n = len(val)
        while i < n:
            if val[i] == '\\':
                if i + 1 < n:
                    cleaned_val.append(val[i:i+2])
                    i += 2
                else:
                    cleaned_val.append('\\\\')
                    i += 1
            elif val[i] == '"':
                cleaned_val.append('\\"')
                i += 1
            else:
                cleaned_val.append(val[i])
                i += 1
        return f'{indent}"{key}": "{"".join(cleaned_val)}"{comma}'

    pattern = r'^(\s*)"([^"]+)"\s*:\s*"(.*)"\s*(,?)\s*$'
    lines = []
    for line in raw_json.splitlines():
        m = re.match(pattern, line)
        if m:
            lines.append(replace_line(m))
        else:
            lines.append(line)
    return "\n".join(lines)


def clean_numeric_null_fields(raw_json: str) -> str:
    raw_json = re.sub(r'"(page|confidence|value)"[ \t]*:[ \t]*"?([0-9\.\-]+)"?[ \t]*(,?[ \t]*)$', r'"\1": \2\3', raw_json, flags=re.MULTILINE)
    raw_json = re.sub(r'"(page|confidence|value)"[ \t]*:[ \t]*"?null"?[ \t]*(,?[ \t]*)$', r'"\1": null\2', raw_json, flags=re.MULTILINE)
    return raw_json


_THOUSANDS_SEPARATOR_PATTERN = re.compile(
    r'("(?:value|confidence|page)"\s*:\s*)("?)(-?\d{1,3}(?:,\d{3})+(?:\.\d+)?)("?)'
)


def clean_thousand_separators(raw_json: str) -> str:
    """
    Models occasionally echo numbers back with thousands-separator commas
    inside a JSON number literal, e.g. "value": 1,112.69. That's invalid
    JSON grammar — the comma terminates the number early and desyncs the
    parser for every key that follows, which previously made the whole
    batch fail (and burn all 3 retries) over a single cosmetic digit group.
    Strip commas from the numeric literal for the known numeric fields,
    whether or not the model also wrapped it in quotes.
    """
    def _strip(m: re.Match) -> str:
        return f"{m.group(1)}{m.group(2)}{m.group(3).replace(',', '')}{m.group(4)}"

    return _THOUSANDS_SEPARATOR_PATTERN.sub(_strip, raw_json)


def repair_truncated_json(raw: str) -> str:
    raw = raw.strip()
    first_brace = raw.find('{')
    if first_brace == -1:
        return raw
    
    json_part = raw[first_brace:]
    
    in_string = False
    escape = False
    stack = []
    
    i = 0
    n = len(json_part)
    while i < n:
        char = json_part[i]
        if in_string:
            if escape:
                escape = False
            elif char == '\\':
                escape = True
            elif char == '"':
                in_string = False
        else:
            if char == '"':
                in_string = True
            elif char == '{':
                stack.append('}')
            elif char == '[':
                stack.append(']')
            elif char == '}':
                if stack and stack[-1] == '}':
                    stack.pop()
            elif char == ']':
                if stack and stack[-1] == ']':
                    stack.pop()
        i += 1
        
    suffix = ""
    if in_string:
        suffix += '"'
        
    temp_json = json_part + suffix
    temp_json_stripped = temp_json.rstrip()
    if temp_json_stripped.endswith(','):
        json_part = temp_json_stripped[:-1]
        suffix = ""
    
    suffix += "".join(reversed(stack))
    return raw[:first_brace] + json_part + suffix


def parse_json_object(raw: str) -> dict[str, Any]:
    # Strip thousands-separator commas from numeric literals up front — this
    # is invalid JSON on its face, so no downstream repair path can recover
    # from it without this running first.
    raw = clean_thousand_separators(raw)

    # Try parsing direct first
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    # Try extracting complete json block using regex \{.*\}
    match = re.search(r"\{.*\}", raw, flags=re.DOTALL)
    if match:
        extracted = match.group(0)
        try:
            return json.loads(extracted)
        except json.JSONDecodeError:
            pass

        # Try cleaning quotes and numeric fields on the extracted block
        cleaned = clean_unescaped_quotes(extracted)
        cleaned = clean_numeric_null_fields(cleaned)
        try:
            parsed = json.loads(cleaned)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

    # If that fails, or if no complete block is found, it might be truncated.
    # Find the first '{' and extract everything from there to the end.
    first_brace = raw.find('{')
    if first_brace != -1:
        extracted = raw[first_brace:]
        
        # Clean quotes and numeric fields
        cleaned = clean_unescaped_quotes(extracted)
        cleaned = clean_numeric_null_fields(cleaned)
        
        # Repair truncation by closing braces
        repaired = repair_truncated_json(cleaned)
        try:
            parsed = json.loads(repaired)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError as exc:
            raise JsonParseFailureError(f"Failed to parse repaired truncated JSON: {exc}. Repaired text: {repaired}") from exc

    raise JsonParseFailureError("LLM output did not contain a valid JSON object or could not be repaired")

