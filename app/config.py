from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application configuration, loaded from environment or a local .env file."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "Life Insurance Outreach Assistant"
    public_base_url: str = "http://localhost:8000"
    database_url: str = "sqlite:///./assistant.db"

    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_phone_number: str = ""
    twilio_validate_signatures: bool = True

    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_use_tls: bool = True
    from_email: str = "assistant@example.com"
    reply_to_email: str = ""

    openai_api_key: str = ""
    openai_text_model: str = "gpt-4o-mini"
    openai_realtime_model: str = "gpt-4o-realtime-preview-2024-12-17"
    openai_realtime_voice: str = "alloy"

    agency_name: str = "Your Agency"
    agency_mailing_address: str = "123 Main St, Suite 100, Anytown, ST 12345"
    assistant_name: str = "Avery"
    licensed_agent_name: str = "a licensed agent"

    # Compliance
    call_window_start_hour: int = 8
    call_window_end_hour: int = 21
    max_contact_attempts: int = 6
    record_calls: bool = True

    @property
    def twilio_configured(self) -> bool:
        return bool(self.twilio_account_sid and self.twilio_auth_token and self.twilio_phone_number)

    @property
    def smtp_configured(self) -> bool:
        return bool(self.smtp_host and self.from_email)

    @property
    def openai_configured(self) -> bool:
        return bool(self.openai_api_key)


@lru_cache
def get_settings() -> Settings:
    return Settings()
