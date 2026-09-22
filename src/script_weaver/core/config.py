"""Application configuration via environment variables."""

from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Global application settings loaded from environment variables."""

    # LLM Provider configuration
    llm_provider: Literal[
        "anthropic", "openai", "deepseek", "glm", "qwen", "openai_compatible"
    ] = "anthropic"
    llm_model: str = "claude-sonnet-4-20250514"
    llm_temperature: float = 0.7
    llm_max_tokens: int = 8192

    # API Keys (provider-specific)
    anthropic_api_key: str | None = None
    openai_api_key: str | None = None
    deepseek_api_key: str | None = None
    glm_api_key: str | None = None
    qwen_api_key: str | None = None

    # OpenAI-compatible endpoint (for Ollama, vLLM, etc.)
    openai_base_url: str | None = None

    # Paths
    data_dir: Path = Path.home() / ".scriptweaver"
    skills_builtin_dir: Path = Path(__file__).resolve().parent.parent / "skills" / "builtin"
    skills_custom_dir: Path = Path.home() / ".scriptweaver" / "skills" / "custom"

    # Agent settings
    agent_max_tool_iterations: int = 15
    agent_max_self_correction_retries: int = 2
    agent_timeout_seconds: int = 120

    model_config = {"env_prefix": "SCRIPTWEAVER_", "env_file": ".env", "env_file_encoding": "utf-8"}

    @property
    def api_key(self) -> str:
        """Return the API key for the configured provider."""
        keys = {
            "anthropic": self.anthropic_api_key,
            "openai": self.openai_api_key,
            "deepseek": self.deepseek_api_key,
            "glm": self.glm_api_key,
            "qwen": self.qwen_api_key,
            "openai_compatible": self.openai_api_key,
        }
        key = keys.get(self.llm_provider)
        if not key:
            raise ValueError(f"No API key set for provider '{self.llm_provider}'. "
                           f"Set SCRIPTWEAVER_{self.llm_provider.upper()}_API_KEY")
        return key


# Global singleton
_settings: Settings | None = None


def get_settings() -> Settings:
    """Get or create the global Settings singleton."""
    global _settings
    if _settings is None:
        _settings = Settings()
        _settings.data_dir.mkdir(parents=True, exist_ok=True)
        _settings.skills_custom_dir.mkdir(parents=True, exist_ok=True)
    return _settings
