"""Entry point: python -m tinker.ui.gradio"""

import os
from .app import build_app

if __name__ == "__main__":
    port = int(os.getenv("TINKER_GRADIO_PORT", "7860"))
    demo = build_app()
    demo.launch(server_port=port, server_name="0.0.0.0", share=False)
