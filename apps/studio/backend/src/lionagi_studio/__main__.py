import uvicorn

from lionagi_studio.app import app
from lionagi_studio.config import HOST, STUDIO_PORT
from lionhub.serve.daemon.settings import _SHUTDOWN_GRACE_SECONDS

if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=STUDIO_PORT, timeout_graceful_shutdown=_SHUTDOWN_GRACE_SECONDS)
