import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv

ROOT = Path(os.getenv("APP_ROOT", str(Path(__file__).resolve().parents[2]))).resolve()


@dataclass
class Config:
    data_dir: Path
    database_url: str = field(default_factory=lambda: os.getenv("DATABASE_URL", ""), repr=False, kw_only=True)
    key_file: Path
    public_url: str = "http://127.0.0.1:17950"
    cookie_secure: bool = True
    prompt_file: Path = ROOT / "prompts/auto_reply.md"
    frontend_dir: Path = ROOT / "frontend/dist"
    ffmpeg_binary: str = "ffmpeg"
    aliyun_access_key_id: str = field(default="", repr=False)
    aliyun_access_key_secret: str = field(default="", repr=False)
    aliyun_sms_sign_name: str = ""
    aliyun_sms_template_code: str = ""
    aliyun_sms_scheme_name: str = ""
    web_bind_host: str = "127.0.0.1"
    web_trusted_proxy_ips: str = "127.0.0.1"

    smtp_host: str = ""
    smtp_port: int = 465
    smtp_tls: str = "ssl"
    smtp_from: str = ""
    smtp_username: str = ""
    smtp_password: str = field(default="", repr=False)
    registration_enabled: bool = True
    auth_code_interval_seconds: int = 60
    auth_code_target_hourly_limit: int = 5
    auth_code_target_daily_limit: int = 10
    auth_code_ip_hourly_limit: int = 10
    auth_code_ip_daily_limit: int = 30
    auth_request_ip_per_minute: int = 30
    icp_filing_number: str = ""
    public_security_filing_number: str = ""
    public_security_filing_url: str = ""

    @property
    def filing(self):
        return dict(
            icp_number=self.icp_filing_number,
            icp_url="https://beian.miit.gov.cn/" if self.icp_filing_number else "",
            public_security_number=self.public_security_filing_number,
            public_security_url=self.public_security_filing_url,
        )

    @property
    def email_configured(self):
        return bool(self.smtp_host and self.smtp_from and self.smtp_tls in ("ssl", "starttls"))

    def __post_init__(self):
        for name in (
            "auth_code_interval_seconds",
            "auth_code_target_hourly_limit",
            "auth_code_target_daily_limit",
            "auth_code_ip_hourly_limit",
            "auth_code_ip_daily_limit",
            "auth_request_ip_per_minute",
        ):
            if type(getattr(self, name)) is not int or getattr(self, name) <= 0:
                raise ValueError(name.upper() + " 必须为正整数")
        origin = urlsplit(self.public_url)
        if (
            origin.scheme not in ("http", "https")
            or not origin.hostname
            or origin.username
            or origin.password
            or origin.path
            or origin.query
            or origin.fragment
        ):
            raise ValueError("PUBLIC_URL 必须配置为不含路径的本站完整源地址")
        if self.public_security_filing_url:
            link = urlsplit(self.public_security_filing_url)
            if (
                link.scheme != "https"
                or link.hostname not in ("beian.mps.gov.cn", "www.beian.gov.cn", "beian.gov.cn")
                or link.username
                or link.password
            ):
                raise ValueError("公安备案链接必须为官方HTTPS地址")
        if not self.database_url.startswith(("postgresql://", "postgres://")):
            raise ValueError("必须配置 PostgreSQL DATABASE_URL")
        from ipaddress import ip_address

        address = ip_address(self.web_bind_host)
        if not (address.is_loopback or address.is_private) or address.is_unspecified:
            raise ValueError("WEB_BIND_HOST 必须为明确的回环或内网地址")
        proxies = self.web_trusted_proxy_ips.split(",")
        if not proxies or any(not value.strip() for value in proxies):
            raise ValueError("WEB_TRUSTED_PROXY_IPS 必须指定代理 IP")
        for value in proxies:
            proxy = ip_address(value.strip())
            if not (proxy.is_loopback or proxy.is_private) or proxy.is_unspecified:
                raise ValueError("可信代理必须为明确的回环或内网 IP")

    @property
    def sms_configured(self):
        return bool(
            self.aliyun_access_key_id
            and self.aliyun_access_key_secret
            and self.aliyun_sms_sign_name
            and self.aliyun_sms_template_code
            and len(self.aliyun_sms_scheme_name) <= 20
        )

    @classmethod
    def from_env(cls):
        load_dotenv(ROOT / ".env")

        def absolute(value):
            path = Path(value)
            return path if path.is_absolute() else ROOT / path

        data = absolute(os.getenv("DATA_DIR", str(ROOT / "data"))).resolve()
        return cls(
            data,
            absolute(os.getenv("MASTER_KEY_FILE", str(data / "master.key"))),
            os.getenv("PUBLIC_URL", "").rstrip("/"),
            os.getenv("COOKIE_SECURE", "true").lower() == "true",
            ffmpeg_binary=os.getenv("FFMPEG_BINARY", "ffmpeg"),
            smtp_host=os.getenv("SMTP_HOST", "").strip(),
            smtp_port=int(os.getenv("SMTP_PORT", "465")),
            smtp_tls=os.getenv("SMTP_TLS", "ssl").strip(),
            smtp_from=os.getenv("SMTP_FROM", "").strip(),
            smtp_username=os.getenv("SMTP_USERNAME", "").strip(),
            smtp_password=os.getenv("SMTP_PASSWORD", ""),
            auth_code_interval_seconds=int(os.getenv("AUTH_CODE_INTERVAL_SECONDS", "60")),
            auth_code_target_hourly_limit=int(os.getenv("AUTH_CODE_TARGET_HOURLY_LIMIT", "5")),
            auth_code_target_daily_limit=int(os.getenv("AUTH_CODE_TARGET_DAILY_LIMIT", "10")),
            auth_code_ip_hourly_limit=int(os.getenv("AUTH_CODE_IP_HOURLY_LIMIT", "10")),
            auth_code_ip_daily_limit=int(os.getenv("AUTH_CODE_IP_DAILY_LIMIT", "30")),
            auth_request_ip_per_minute=int(os.getenv("AUTH_REQUEST_IP_PER_MINUTE", "30")),
            registration_enabled=os.getenv("AUTH_REGISTRATION_ENABLED", "true").lower() == "true",
            icp_filing_number=os.getenv("ICP_FILING_NUMBER", "").strip(),
            public_security_filing_number=os.getenv("PUBLIC_SECURITY_FILING_NUMBER", "").strip(),
            public_security_filing_url=os.getenv("PUBLIC_SECURITY_FILING_URL", "").strip(),
            aliyun_access_key_id=os.getenv("ALIBABA_CLOUD_ACCESS_KEY_ID", "").strip(),
            aliyun_access_key_secret=os.getenv("ALIBABA_CLOUD_ACCESS_KEY_SECRET", "").strip(),
            aliyun_sms_sign_name=os.getenv("ALIYUN_SMS_SIGN_NAME", "").strip(),
            aliyun_sms_template_code=os.getenv("ALIYUN_SMS_TEMPLATE_CODE", "").strip(),
            aliyun_sms_scheme_name=os.getenv("ALIYUN_SMS_SCHEME_NAME", "").strip(),
            web_bind_host=os.getenv("WEB_BIND_HOST", "127.0.0.1").strip(),
            web_trusted_proxy_ips=os.getenv("WEB_TRUSTED_PROXY_IPS", "127.0.0.1").strip(),
        )
