from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Subtitle Manager"
    host: str = "0.0.0.0"
    port: int = 8080
    database_path: str = "/app/data/subtitle-manager.sqlite3"
    media_root: str = "/media"
    test_mode: bool = True
    open_subtitles_api_key: str = ""
    open_subtitles_username: str = ""
    open_subtitles_password: str = ""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)


settings = Settings()
