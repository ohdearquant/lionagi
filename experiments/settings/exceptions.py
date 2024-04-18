import logging

# Configure logging
logging.basicConfig(
    filename="lionagi_tool_manager.log",
    level=logging.INFO,
    format="%(asctime)s:%(levelname)s:%(message)s",
)


# Custom exception classes
class LionAGIBaseException(Exception):
    """Base class for exceptions in the LionAGI tools manager."""

    def __init__(self, message, errors=None):
        super().__init__(message)
        self.errors = errors


class APIException(LionAGIBaseException):
    """Exception raised for errors in communication with external API services."""

    def __init__(self, message, errors=None):
        super().__init__(message, errors)


class ServiceException(LionAGIBaseException):
    """Exception raised for errors in endpoint configuration."""

    def __init__(self, message, errors=None):
        super().__init__(message, errors)

    @classmethod
    def unavailable(cls, endpoint, service, err_msg=None):
        msg = f"{endpoint} is currently unavailable"
        if err_msg:
            msg += f": {err_msg}"
        else:
            msg += f" for {service.__class__.__name__}"
        return cls(msg)


# class ActionException(LionAGIBaseException):
#     """Exception raised for errors in action invocation."""
#     def __init__(self, message, errors=None):
#         super().__init__(message, errors)

#     @classmethod
#     def


# class InferenceException(LionAGIBaseException):
#     """Exception raised for errors in inference."""
#     def __init__(self, message, errors=None):
#         super().__init__(message, errors)
