"""电商商品与客服工作台的配置模块。

用途：集中读取应用名称、数据库连接、采集超时和运行产物目录等配置，
支持通过 .env 或环境变量切换 SQLite 与 MySQL，避免业务代码散落配置。
"""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "电商商品与客服工作台"
    database_url: str = "sqlite:///./data/dianshang.db"
    crawl_timeout_seconds: int = 10
    crawl_max_retries: int = 2
    artifacts_dir: str = "artifacts"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    @property
    def project_root(self) -> Path:
        return Path(__file__).resolve().parent.parent

    def resolve_path(self, value: str) -> Path:
        path = Path(value)
        return path if path.is_absolute() else self.project_root / path


@lru_cache
def get_settings() -> Settings:
    return Settings()
