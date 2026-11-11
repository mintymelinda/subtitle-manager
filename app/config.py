from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Subtitle Manager"
    database_path: str = "/app/data/subtitle-manager.sqlite3"
    media_root: str = "/media"
    test_mode: bool = False
    subtitle_staging_path: str = "/app/data/staged-subtitles"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)


settings = Settings()
