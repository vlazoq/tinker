"""
ui/
===
All Tinker user interfaces live under this package.

  ui/core.py     — shared data layer: constants, DB helpers, schemas.
                   Single source of truth imported by all three UI variants.

  ui/web/        — FastAPI + React SPA  (python -m tinker.ui.web, port 8082)
  ui/streamlit/  — Streamlit control panel  (python -m tinker.ui.streamlit, port 8501)
  ui/gradio/     — Gradio control panel  (python -m tinker.ui.gradio, port 7860)

All three variants read/write the same on-disk state (SQLite databases, JSON
config files) via helpers in ui/core.py.  To add a new config field, flag, or
subsystem enum, edit ui/core.py once — all three UIs pick it up automatically.
"""
