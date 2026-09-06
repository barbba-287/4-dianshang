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

    # S1 任务工程化
    worker_concurrency: int = 2
    worker_lease_seconds: int = 60
    worker_heartbeat_seconds: int = 5
    worker_max_attempts: int = 3
    worker_poll_seconds: float = 0.5

    # S2 文档导入
    upload_max_bytes: int = 20 * 1024 * 1024
    upload_chunk_bytes: int = 64 * 1024
    upload_max_zip_members: int = 2000
    upload_max_zip_uncompressed_bytes: int = 100 * 1024 * 1024
    upload_max_pdf_pages: int = 500
    imports_dir: str = "uploads"

    # S3 RAG 检索
    embedder_dim: int = 256
    vector_store_path: str = "data/vectors.json"
    rag_top_k: int = 5
    rag_min_score: float = 0.2
    external_snapshot_stale_after_seconds: int = 24 * 60 * 60
    external_sync_run_stale_after_seconds: int = 15 * 60
    taobao_adapter_enabled: bool = False
    taobao_api_base_url: str = ""
    taobao_app_key: str = ""
    taobao_app_secret_ref: str = ""
    taobao_access_token_ref: str = ""
    taobao_request_timeout_seconds: int = 15
    taobao_max_page_size: int = 100

    # S7 API 安全（默认关闭以保留本地演示兼容）
    api_auth_enabled: bool = False
    api_key: str = ""
    api_tenant_id: str = "default"
    api_user_id: str = "api-user"

    # 员工登录与角色工作台（启用后使用数据库 session，不把 API key 放进浏览器）
    employee_auth_enabled: bool = True
    demo_mode_enabled: bool = False
    session_cookie_name: str = "dianshang_session"
    session_ttl_seconds: int = 8 * 60 * 60
    session_cookie_secure: bool = False
    auth_bootstrap_login: str = ""
    auth_bootstrap_password: str = ""
    auth_bootstrap_display_name: str = "管理员"
    auth_bootstrap_tenant_key: str = "default"
    auth_bootstrap_workspace_name: str = "默认商家"

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
