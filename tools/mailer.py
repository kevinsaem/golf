"""이메일 발송 유틸. SMTP 설정이 없으면 조용히 건너뛴다(앱은 계속 동작).

전원 등록 완료 시 주최자에게 '확정하세요' 알림을 보내는 데 쓴다.
"""

from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage

from app import config


def send_email(to: str, subject: str, body_text: str, body_html: str | None = None) -> bool:
    """메일 한 통 발송. 성공 True / (미설정·실패) False."""
    if not config.MAIL_ENABLED:
        print("[mailer] SMTP 미설정 → 메일 건너뜀")
        return False
    if not to:
        return False

    msg = EmailMessage()
    msg["From"] = config.MAIL_FROM
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body_text)
    if body_html:
        msg.add_alternative(body_html, subtype="html")

    try:
        context = ssl.create_default_context()
        if config.SMTP_PORT == 465:
            with smtplib.SMTP_SSL(config.SMTP_HOST, config.SMTP_PORT, context=context, timeout=15) as s:
                s.login(config.SMTP_USER, config.SMTP_PASSWORD)
                s.send_message(msg)
        else:
            with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=15) as s:
                s.starttls(context=context)
                s.login(config.SMTP_USER, config.SMTP_PASSWORD)
                s.send_message(msg)
        print(f"[mailer] 발송 성공 → {to}")
        return True
    except Exception as e:  # 메일 실패가 서비스 장애로 번지지 않게
        print(f"[mailer] 발송 실패: {e}")
        return False
