"""SMTP delivery for account verification codes."""

from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr

from app.core.config import Settings


class MailConfigurationError(RuntimeError):
    pass


class VerificationMailer:
    def __init__(self, config: Settings) -> None:
        self.config = config

    def send_registration_code(self, recipient: str, code: str) -> None:
        sender = self.config.mail_from.strip() or self.config.mail_username.strip()
        if not self.config.mail_server.strip() or not sender:
            raise MailConfigurationError("Email delivery is not configured.")
        message = EmailMessage()
        message["Subject"] = "AI Business Studio 注册验证码"
        message["From"] = formataddr(("AI Business Studio", sender))
        message["To"] = recipient
        message.set_content(
            f"你的注册验证码是：{code}\n\n验证码 {self.config.verification_code_ttl_minutes} 分钟内有效。"
            "如果不是你本人操作，请忽略此邮件。"
        )
        message.add_alternative(
            "<div style=\"font-family:Arial,sans-serif;color:#1f2937;line-height:1.6\">"
            "<h2 style=\"margin:0 0 16px\">AI Business Studio</h2>"
            "<p>你的注册验证码是：</p>"
            f"<p style=\"font-size:28px;font-weight:700;letter-spacing:6px\">{code}</p>"
            f"<p>验证码 {self.config.verification_code_ttl_minutes} 分钟内有效。"
            "如果不是你本人操作，请忽略此邮件。</p></div>",
            subtype="html",
        )

        context = ssl.create_default_context()
        timeout = max(3, self.config.mail_timeout_seconds)
        if self.config.mail_ssl_tls:
            with smtplib.SMTP_SSL(
                self.config.mail_server,
                self.config.mail_port,
                timeout=timeout,
                context=context,
            ) as client:
                self._authenticate_and_send(client, sender, recipient, message)
            return
        with smtplib.SMTP(
            self.config.mail_server,
            self.config.mail_port,
            timeout=timeout,
        ) as client:
            client.ehlo()
            if self.config.mail_starttls:
                client.starttls(context=context)
                client.ehlo()
            self._authenticate_and_send(client, sender, recipient, message)

    def _authenticate_and_send(
        self,
        client: smtplib.SMTP,
        sender: str,
        recipient: str,
        message: EmailMessage,
    ) -> None:
        if self.config.mail_use_credentials:
            username = self.config.mail_username.strip()
            password = self.config.mail_password
            if not username or not password:
                raise MailConfigurationError("Email credentials are not configured.")
            client.login(username, password)
        client.send_message(message, from_addr=sender, to_addrs=[recipient])
