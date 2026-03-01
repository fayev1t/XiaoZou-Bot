"""LLM configuration and initialization."""

from typing import TYPE_CHECKING

from pydantic_settings import BaseSettings

from qqbot.core.logging import get_logger

logger = get_logger(__name__)

if TYPE_CHECKING:
    from langchain_core.language_model.llm import LLM


class LLMConfig(BaseSettings):
    """LLM configuration from environment."""

    llm_provider: str = "deepseek"  # deepseek, openai, claude, etc.
    llm_api_key: str = ""
    llm_model: str = "deepseek-chat"
    llm_temperature: float = 0.7

    class Config:
        env_file = ".env.dev"
        env_file_encoding = "utf-8"
        case_sensitive = False
        extra = "ignore"


async def create_llm(temperature: float | None = None) -> "LLM | None":
    """Create and return LLM instance based on config.

    Args:
        temperature: Override the default temperature from config

    Returns:
        LLM instance or None if configuration is incomplete
    """
    config = LLMConfig()
    
    # Use provided temperature or fallback to config default
    temp = temperature if temperature is not None else config.llm_temperature

    if not config.llm_api_key:
        logger.warning("LLM_API_KEY not configured")
        return None

    if config.llm_provider == "deepseek":
        try:
            from langchain_openai import ChatOpenAI

            # DeepSeek compatible with OpenAI API
            llm = ChatOpenAI(
                model_name=config.llm_model,
                api_key=config.llm_api_key,
                base_url="https://api.deepseek.com/v1",
                temperature=temp,
            )
            return llm
        except ImportError as e:
            logger.error(f"Failed to import ChatOpenAI: {e}")
            return None

    elif config.llm_provider == "openai":
        try:
            from langchain_openai import ChatOpenAI

            llm = ChatOpenAI(
                model_name=config.llm_model,
                api_key=config.llm_api_key,
                temperature=temp,
            )
            return llm
        except ImportError as e:
            logger.error(f"Failed to import ChatOpenAI: {e}")
            return None

    else:
        logger.error(f"Unknown LLM provider: {config.llm_provider}")
        return None
