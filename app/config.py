from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://jobs:jobs@localhost:5433/jobs"
    user_agent: str = "IndiaJobsDemoBot/0.1 (local-demo)"
    openrouter_api_key: str = ""
    openrouter_model: str = "openrouter/free"
    openrouter_daily_budget: int = 40
    openai_api_key: str = ""
    openai_model: str = "gpt-5"
    openai_daily_budget: int = 400
    adzuna_app_id: str = ""
    adzuna_app_key: str = ""
    crawl_concurrency: int = 5
    request_timeout_seconds: int = 20
    jobs_agent_token: str = ""


settings = Settings()
