import uvicorn

from lionagi.studio.app import app
from lionagi.studio.config import HOST, STUDIO_PORT
from lionagi.studio.daemon_log import daemon_log_handler

if __name__ == "__main__":
    with daemon_log_handler():
        uvicorn.run(app, host=HOST, port=STUDIO_PORT)
