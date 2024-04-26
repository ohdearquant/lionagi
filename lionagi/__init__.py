"""
lionagi is an intelligent automation framework.
"""

import logging
from .version import __version__
from dotenv import load_dotenv

# from .core import direct, Branch, Session, func_to_tool
# from .integrations.provider.services import Services
# from .integrations.chunker.chunk import chunk
# from .integrations.loader.load import load
# from .experimental.work import work, Worker
# from .experimental.report import Report, Form

# from pydantic import Field


logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
load_dotenv()
