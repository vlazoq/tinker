"""
bootstrap/ — Application startup modules.

Each module has a single responsibility extracted from the original main.py:

  logging_config  — Configure loguru / stdlib logging.
  components      — Build core AI / storage components.
  enterprise_stack — Build resilience / observability components.
  health          — Startup health-check and asyncio error handler.

Keeping these concerns separate means each module can be read, tested,
and changed without touching unrelated startup code (SRP).
"""
