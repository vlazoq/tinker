"""Entry point: python -m tinker.ui.web"""

import os
import uvicorn

if __name__ == "__main__":
    port = int(os.getenv("TINKER_WEBUI_PORT", "8082"))
    uvicorn.run(
        "tinker.ui.web.app:app",
        host="0.0.0.0",
        port=port,
        reload=False,
        log_level="info",
    )
