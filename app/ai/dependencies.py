from fastapi import Depends

from .client import AIClient, DisabledAIClient, LlamaCppClient
from .config import AISettings, get_ai_settings


def get_ai_client(settings: AISettings = Depends(get_ai_settings)) -> AIClient:
    if not settings.enabled:
        return DisabledAIClient()
    return LlamaCppClient(settings)
