from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    app_name: str = "RouterVault"
    app_env: str = "production"
    app_secret: str
    admin_api_key: str
    admin_username: str = "admin"
    admin_password: str
    database_url: str
    s3_endpoint: str
    s3_access_key: str
    s3_secret_key: str
    s3_bucket: str = "routervault"
    s3_region: str = "us-east-1"
    sftp_user: str = "backup"
    sftp_password: str
    sftp_port: int = 2222
    ingest_dir: str = "/ingest"
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

settings = Settings()
