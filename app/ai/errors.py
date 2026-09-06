class AIError(Exception):
    """Base error that is safe to translate into an AI-only response."""


class AIDisabledError(AIError):
    pass


class AIRequestTooLargeError(AIError):
    pass


class AITimeoutError(AIError):
    pass


class AIUnavailableError(AIError):
    pass


class AIProtocolError(AIError):
    pass


class AIResponseValidationError(AIError):
    pass
