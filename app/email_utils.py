import logging
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText


SMTP_USER = os.environ.get('SMTP_USER', '')
SMTP_PASS = os.environ.get('SMTP_PASS', '')
logger = logging.getLogger(__name__)


def send_job_alert(to_email, company_name, new_jobs):
    if not SMTP_USER or not SMTP_PASS:
        logger.warning("SMTP is not configured; alert email was not sent")
        return False
    if not to_email:
        logger.warning("No recipient email; alert email was not sent")
        return False

    subject = f"Job Tracker: {len(new_jobs)} new job{'s' if len(new_jobs) > 1 else ''} at {company_name}"
    lines = [f"New matching job{'s' if len(new_jobs) > 1 else ''} found at {company_name}:\n"]
    for job in new_jobs:
        lines.append(f"  - {job['title']}")
        lines.append(f"    Location: {job['location']}")
        if job['url']:
            lines.append(f"    Apply: {job['url']}")
        lines.append("")
    lines.append("--\nJob Tracker")

    msg = MIMEMultipart()
    msg['From'] = SMTP_USER
    msg['To'] = to_email
    msg['Subject'] = subject
    msg.attach(MIMEText("\n".join(lines), 'plain'))

    try:
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
            server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(SMTP_USER, to_email, msg.as_string())
        logger.info("Sent alert to %s for %s (%s jobs)", to_email, company_name, len(new_jobs))
        return True
    except Exception:
        logger.exception("Failed to send alert email")
        return False
