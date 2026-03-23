"""
Entry point:  python -m tinker.ui.streamlit
Internally runs:  streamlit run tinker/ui/streamlit/app.py
"""

import os
import subprocess
import sys
from pathlib import Path

if __name__ == "__main__":
    port = str(int(os.getenv("TINKER_STREAMLIT_PORT", "8501")))
    app = Path(__file__).parent / "app.py"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "streamlit",
            "run",
            str(app),
            "--server.port",
            port,
            "--server.address",
            "0.0.0.0",
            "--browser.gatherUsageStats",
            "false",
        ]
    )
