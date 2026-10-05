"""The served actor (ADR-0021): an actor's offers served over HTTP on loopback.

    lion = Lion()
    lion.offer(profile, backend)
    asyncio.run(lion.serve(actor))

`Service` holds the offers and the runs it started and knows no HTTP; `serve` carries one over FastAPI
on uvicorn. Both need the `serve` extra.
"""

from .service import Offer, ServeError, Service
from .transport import Lion, app, serve

__all__ = ("Lion", "Offer", "ServeError", "Service", "app", "serve")
