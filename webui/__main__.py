"""Entry point: python -m tinker.webui"""
import os
import uvicorn

if __name__ == "__main__":
    port = int(os.getenv("TINKER_WEBUI_PORT", "8082"))
    uvicorn.run(
        "tinker.webui.app:app",
        host="0.0.0.0",
        port=port,
        reload=False,
        log_level="info",
    )
