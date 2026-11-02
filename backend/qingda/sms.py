"""阿里云号码认证；不返回、保存或记录明文验证码，不自动重试。"""

import asyncio
import json

from alibabacloud_dypnsapi20170525 import models
from alibabacloud_dypnsapi20170525.client import Client
from alibabacloud_tea_openapi.models import Config as SdkConfig
from alibabacloud_tea_util.models import RuntimeOptions


class SmsUnavailable(Exception):
    """供应商异常只向业务层暴露稳定原因，不携带 SDK 请求内容。"""


class AliyunSms:
    def __init__(self, config):
        self.config = config
        self.client = None
        if config.sms_configured:
            self.client = Client(
                SdkConfig(
                    access_key_id=config.aliyun_access_key_id,
                    access_key_secret=config.aliyun_access_key_secret,
                    endpoint="dypnsapi.aliyuncs.com",
                    region_id="cn-hangzhou",
                )
            )

    async def _call(self, operation, request):
        if self.client is None:
            raise SmsUnavailable() from None
        try:
            response = await asyncio.wait_for(
                getattr(self.client, operation)(
                    request,
                    RuntimeOptions(
                        autoretry=False,
                        max_attempts=1,
                        connect_timeout=3000,
                        read_timeout=8000,
                    ),
                ),
                timeout=12,
            )
            body = response.body
            if body is None or body.code != "OK" or body.success is not True or body.model is None:
                raise SmsUnavailable()
            return body.model
        except Exception:
            # SDK 异常可能包含手机号、验证码、签名及请求参数，禁止向上层传播。
            raise SmsUnavailable() from None

    async def send(self, phone, challenge_id):
        await self._call(
            "send_sms_verify_code_with_options_async",
            models.SendSmsVerifyCodeRequest(
                phone_number=phone,
                country_code="86",
                out_id=challenge_id,
                scheme_name=self.config.aliyun_sms_scheme_name or None,
                sign_name=self.config.aliyun_sms_sign_name,
                template_code=self.config.aliyun_sms_template_code,
                template_param=json.dumps({"code": "##code##", "min": "5"}),
                code_length=6,
                code_type=1,
                valid_time=300,
                interval=self.config.auth_code_interval_seconds,
                duplicate_policy=1,
                return_verify_code=False,
                auto_retry=0,
            ),
        )

    async def verify(self, phone, challenge_id, code):
        result = await self._call(
            "check_sms_verify_code_with_options_async",
            models.CheckSmsVerifyCodeRequest(
                phone_number=phone,
                country_code="86",
                out_id=challenge_id,
                scheme_name=self.config.aliyun_sms_scheme_name or None,
                verify_code=code,
            ),
        )
        return result.verify_result == "PASS" and result.out_id == challenge_id
