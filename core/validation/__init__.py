"""
core/validation/ — Input validation and sanitization for Tinker.

Validates all data at system boundaries to prevent:
  - Prompt injection attacks (malicious content in problem statements)
  - Path traversal vulnerabilities (unsafe file paths from AI output)
  - Schema violations (malformed JSON from AI responses)
  - Oversized inputs (unbounded strings that waste compute or cause OOM)

Modules:
  input_validator  — Validates and sanitises user-supplied inputs
                     (problem statements, config values, file paths)
  schema_validator — Validates AI model outputs against expected schemas
"""
