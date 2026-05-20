import uvicorn
from apps.studio.server.app import app
from apps.studio.server.config import HOST, STUDIO_PORT

if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=STUDIO_PORT)
