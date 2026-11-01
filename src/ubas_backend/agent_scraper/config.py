import pathlib

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    headless_mode: bool = False

    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_db: int = 0
    redis_password: str | None = None

    cache_ttl_seconds: int = 2 * 24 * 60 * 60

    base_dir: pathlib.Path = pathlib.Path(__file__).resolve().parent

    @property
    def captured_dir(self) -> pathlib.Path:
        return self.base_dir / "captured_data"

    @property
    def user_data_dir(self) -> pathlib.Path:
        return self.base_dir / "browser_user_data"

    @property
    def export_dir(self) -> pathlib.Path:
        return self.base_dir / "exported_reports"

    def ensure_dirs(self) -> None:
        for directory in (self.captured_dir, self.user_data_dir, self.export_dir):
            directory.mkdir(exist_ok=True)


settings = Settings()
settings.ensure_dirs()
