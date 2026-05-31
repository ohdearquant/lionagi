import uvicorn

from lionagi.studio.app import app
from lionagi.studio.config import HOST, STUDIO_PORT

if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=STUDIO_PORT)
