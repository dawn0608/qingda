"""SMTP 验证码：TLS、有限等待、无自动重试，不输出供应商异常或验证码。"""

import asyncio
import smtplib
import ssl
from email.message import EmailMessage

from .sms import SmsUnavailable


class EmailCodes:
    def __init__(self, config):
        self.config = config
        self.slots = asyncio.Semaphore(2)

    def _send(self, recipient, code):
        c = self.config
        message = EmailMessage()
        message["From"] = c.smtp_from
        message["To"] = recipient
        message["Subject"] = "青答验证码"
        message.set_content(f"你的青答验证码为 {code}，5 分钟内有效。请勿向他人提供验证码。")
        cls = smtplib.SMTP_SSL if c.smtp_tls == "ssl" else smtplib.SMTP
        options = {"timeout": 3}
        if c.smtp_tls == "ssl":
            options["context"] = ssl.create_default_context()
        with cls(c.smtp_host, c.smtp_port, **options) as client:
            if c.smtp_tls == "starttls":
                client.starttls(context=ssl.create_default_context())
            if c.smtp_username:
                client.login(c.smtp_username, c.smtp_password)
            client.send_message(message)

    async def send(self, recipient, code):
        if not self.config.email_configured:
            raise SmsUnavailable() from None
        try:
            await asyncio.wait_for(self.slots.acquire(), 2)
        except asyncio.TimeoutError:
            raise SmsUnavailable() from None
        task = asyncio.create_task(asyncio.to_thread(self._send, recipient, code))
        task.add_done_callback(lambda _: self.slots.release())
        try:
            await asyncio.wait_for(asyncio.shield(task), 12)
        except Exception:
            # 后台线程结束才归还额度；调用方将挑战终止，不重试不确定发送。
            task.add_done_callback(lambda future: future.exception() if not future.cancelled() else None)
            raise SmsUnavailable() from None
