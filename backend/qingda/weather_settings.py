"""逐用户天气配置；一次读取完整凭据，不使用共享环境兜底。"""

import re

from .model_settings import masked_key

HOST = re.compile(r"[a-zA-Z0-9-]+(?:\.[a-zA-Z0-9-]+)*\.qweatherapi\.com")


def validate_host(value):
    value = value.strip().lower()
    if len(value) > 253 or not HOST.fullmatch(value):
        raise ValueError("API Host 须为和风天气域名，不包含协议、端口或路径")
    return value


def save(manager, api_host, api_key):
    host = validate_host(api_host)
    key = (api_key or "").strip()
    if key and not re.fullmatch(r"[!-~]{1,1000}", key):
        raise ValueError("和风天气 API 密钥格式不正确")
    if not manager.vault:
        raise ValueError("密钥保存不可用")
    with manager.store.transaction() as conn:
        conn.execute("SELECT id FROM users WHERE id=current_setting('app.user_id')::bigint FOR UPDATE")
        previous = manager.store.setting("weather", {}, conn)
        encrypted = previous.get("api_key", "")
        if not key and not encrypted:
            raise ValueError("请填写和风天气 API 密钥")
        if key:
            encrypted = manager.vault.encrypt(key)
        manager.store.set_setting("weather", {"api_host": host, "api_key": encrypted}, conn)
        manager.store.audit("weather_updated", conn=conn)


def credentials(manager):
    saved = manager.store.setting("weather", {})
    return saved.get("api_host", ""), manager.vault.decrypt(saved["api_key"]) if saved.get("api_key") else ""


def public_settings(manager):
    host, key = credentials(manager)
    return {"api_host": host, "key_configured": bool(host and key), "key_preview": masked_key(key)}
