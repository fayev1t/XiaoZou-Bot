"""LLM configuration and initialization."""

from typing import TYPE_CHECKING

from pydantic_settings import BaseSettings

from qqbot.core.logging import get_logger
from qqbot.core.settings import get_settings_env_files

logger = get_logger(__name__)

if TYPE_CHECKING:
    from langchain_core.language_model.llm import LLM


class LLMConfig(BaseSettings):
    llm_provider: str = "openai"
    llm_api_key: str = ""
    llm_model: str = ""
    llm_temperature: float = 0.7
    llm_base_url: str = ""
    llm_max_tokens: int | None = None

    class Config:
        env_file = get_settings_env_files()
        env_file_encoding = "utf-8"
        case_sensitive = False
        extra = "ignore"


async def create_llm(temperature: float | None = None) -> "LLM | None":
    config = LLMConfig()
    temp = temperature if temperature is not None else config.llm_temperature

    if not config.llm_api_key:
        logger.warning("LLM_API_KEY not configured")
        return None

    if not config.llm_model:
        logger.warning("LLM_MODEL not configured")
        return None

    if config.llm_provider not in {"deepseek", "openai"}:
        logger.error(f"Unknown LLM provider: {config.llm_provider}")
        return None

    try:
        from langchain_openai import ChatOpenAI
    except ImportError as e:
        logger.error(f"Failed to import ChatOpenAI: {e}")
        return None

    llm_kwargs = {
        "model_name": config.llm_model,
        "api_key": config.llm_api_key,
        "temperature": temp,
    }
    if config.llm_provider == "openai":
        llm_kwargs["streaming"] = True
    if config.llm_max_tokens is not None:
        llm_kwargs["max_tokens"] = config.llm_max_tokens

    base_url = config.llm_base_url.strip()
    if config.llm_provider == "deepseek":
        llm_kwargs["base_url"] = base_url or "https://api.deepseek.com/v1"
    elif base_url:
        llm_kwargs["base_url"] = base_url

    logger.info(
        "[llm] create client",
        extra={
            "provider": config.llm_provider,
            "model": config.llm_model,
            "base_url": llm_kwargs.get("base_url", ""),
            "max_tokens": config.llm_max_tokens,
            "temperature": temp,
            "streaming": llm_kwargs.get("streaming", False),
        },
    )

    return ChatOpenAI(**llm_kwargs)
