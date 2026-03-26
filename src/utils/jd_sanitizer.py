"""
JD Safety Pipeline — Stage 1: Hard Sanitization
Strips prompt injection, hidden Unicode, LaTeX commands, and suspicious patterns.
Runs BEFORE any LLM sees the text. Zero cost, instant.
"""
import re
import unicodedata


# Prompt injection patterns to detect and remove
_INJECTION_PATTERNS = [
    (re.compile(
        r'(?i)(ignore|disregard|forget|override|bypass)\s+(all\s+)?'
        r'(previous|prior|above|earlier|system)\s+(instructions|rules|prompts|guidelines)'
    ), "injection_ignore_instructions"),
    (re.compile(r'(?i)you\s+are\s+now\s+(a|an|my)'), "injection_role_reassignment"),
    (re.compile(r'(?i)(system\s*prompt|hidden\s*instruction|secret\s*instruction)'),
     "injection_system_prompt_reference"),
    (re.compile(
        r'(?i)(print|output|reveal|show|display)\s+(your|the|all)\s+'
        r'(system|initial|original|secret)\s+(prompt|instructions|rules)'
    ), "injection_prompt_extraction"),
    (re.compile(
        r'(?i)when\s+(generating|creating|writing|tailoring)\s+(the\s+)?'
        r'(cv|resume|cover\s*letter)'
    ), "injection_document_manipulation"),
    (re.compile(
        r'(?i)('
        r'subprocess[\s.(]|'      # subprocess module (never in a real JD)
        r'os\.system\s*\(|'       # os.system() call
        r'__import__\s*\(|'       # __import__() call
        r'eval\s*\(|'             # eval() call
        r'exec\s*\('              # exec() call
        r')'
    ), "injection_code_execution"),
]

# LaTeX commands that should never appear in a job description
_LATEX_PATTERNS = [
    r'\\write18', r'\\immediate', r'\\input\{/', r'\\include\{/',
    r'\\openin', r'\\openout', r'\\catcode', r'\\verbatiminput',
    r'\\newwrite', r'\\closeout',
]

# Unicode ranges used to hide text (invisible to humans, visible to LLMs)
_HIDDEN_UNICODE_RANGES = set(range(0x200B, 0x2070)) | {0xFEFF, 0xFFFE}

MAX_JD_LENGTH = 50_000


def sanitize_jd(text: str) -> dict:
    """
    Sanitize a job description for safe processing.

    Returns:
        {
            "clean_text": str,     # Sanitized text safe for LLM processing
            "flags": list[str],    # What was found and removed
            "blocked": bool,       # If True, skip this JD entirely
        }
    """
    if not isinstance(text, str):
        return {"clean_text": "", "flags": ["invalid_input_type"], "blocked": True}

    flags = []

    # 1. Strip hidden Unicode characters
    hidden_count = 0
    clean_chars = []
    for char in text:
        code = ord(char)
        cat = unicodedata.category(char)
        if cat.startswith('C') and char not in '\n\r\t':
            hidden_count += 1
        elif code in _HIDDEN_UNICODE_RANGES:
            hidden_count += 1
        else:
            clean_chars.append(char)
    text = ''.join(clean_chars)
    if hidden_count > 0:
        flags.append(f"stripped_hidden_unicode_chars:{hidden_count}")

    # 2. Detect and remove prompt injection patterns
    for pattern, flag_name in _INJECTION_PATTERNS:
        if pattern.search(text):
            flags.append(flag_name)
            text = pattern.sub('[REMOVED]', text)

    # 3. Detect LaTeX commands in JD (should never be in a job posting)
    for pattern in _LATEX_PATTERNS:
        if re.search(pattern, text):
            flags.append(f"latex_in_jd:{pattern}")
            text = re.sub(pattern, '[REMOVED]', text)

    # 4. Check suspicious content density
    if hidden_count > 10 and len(text) > 100:
        flags.append("high_hidden_content_ratio")

    # 5. Truncate
    if len(text) > MAX_JD_LENGTH:
        text = text[:MAX_JD_LENGTH] + "\n[TRUNCATED]"
        flags.append("truncated")

    # Block entirely if severe injection detected
    severe = {"injection_code_execution", "injection_document_manipulation"}
    blocked = bool(severe & set(flags))

    return {
        "clean_text": text.strip(),
        "flags": flags,
        "blocked": blocked,
    }
