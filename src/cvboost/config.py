"""Runtime settings, read from the environment."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str = "") -> str:
    """An environment value; empty or blank means "not set" (`KEY=` lines of a copied .env)."""
    value = os.environ.get(name, "").strip()
    return value or default


@dataclass(frozen=True)
class Settings:
    db_path: Path
    log_level: str
    rules_path: Path
    llm_config_path: Path
    worker_config_path: Path = Path("config/worker.yaml")
    # repr=False keeps the bot token out of repr(settings), logs and tracebacks
    telegram_token: str | None = field(default=None, repr=False)
    telegram_chat_id: int | None = None
    # web panel (#6); secrets stay out of repr
    kits_override: Path | None = None  # CVBOOST_KITS_DIR
    panel_url: str | None = None  # CVBOOST_PANEL_URL: link in "CV ready" messages
    web_user: str | None = field(default=None, repr=False)
    web_password: str | None = field(default=None, repr=False)
    web_hosts: tuple[str, ...] = ("localhost", "127.0.0.1")
    web_secret: str | None = field(default=None, repr=False)
    web_trust_network: bool = False
    resume_dir: Path = Path("config/resume")  # profile.md, gaps.yaml, photo.jpg, resume.css
    companies_path: Path = Path("config/companies.yaml")  # ATS companies (#18)
    profile_path: Path = Path("config/profile.yaml")  # who the prompts are about (#58)
    outreach_path: Path = Path("config/outreach.yaml")  # approved public links for CRM drafts (#39)
    # alerts mailbox (#17); the password stays out of repr
    imap_user: str | None = None
    imap_password: str | None = field(default=None, repr=False)
    imap_host: str = "imap.gmail.com"
    imap_mailbox: str = "INBOX"
    # the owner's own addresses whose forwards are unwrapped (CVBOOST_MAIL_FORWARDERS); none = off
    mail_forwarders: tuple[str, ...] = ()

    def imap_creds(self):  # -> ImapCreds | None
        """Credentials of the alerts mailbox, None when not configured."""
        from cvboost.mail.imap import ImapCreds

        if not self.imap_user or not self.imap_password:
            return None
        return ImapCreds(self.imap_user, self.imap_password, self.imap_host, self.imap_mailbox)

    @property
    def data_dir(self) -> Path:
        """Directory of the DB file: lock files live next to it."""
        return self.db_path.parent

    @property
    def kits_dir(self) -> Path:
        """Application kits (resume.md / pdf / notes) live next to the DB, on the data volume."""
        return self.kits_override or Path(self.db_path).parent / "kits"

    @classmethod
    def from_env(cls) -> Settings:
        chat = _env("TELEGRAM_CHAT_ID")
        try:
            chat_id = int(chat) if chat else None
        except ValueError:
            raise ValueError("TELEGRAM_CHAT_ID must be an integer") from None
        panel = _env("CVBOOST_PANEL_URL").rstrip("/")
        if panel and not re.fullmatch(r"https?://[^\s<>\"']+", panel):
            raise ValueError("CVBOOST_PANEL_URL must be an http(s) URL")
        kits = _env("CVBOOST_KITS_DIR")
        user = _env("CVBOOST_WEB_USER") or None
        password = os.environ.get("CVBOOST_WEB_PASSWORD", "") or None
        if (user is None) != (password is None):
            raise ValueError("set both CVBOOST_WEB_USER and CVBOOST_WEB_PASSWORD, or neither")
        imap_user = _env("CVBOOST_IMAP_USER") or None
        imap_password = os.environ.get("CVBOOST_IMAP_PASSWORD", "") or None
        if (imap_user is None) != (imap_password is None):
            raise ValueError("set both CVBOOST_IMAP_USER and CVBOOST_IMAP_PASSWORD, or neither")
        imap_host = _env("CVBOOST_IMAP_HOST") or "imap.gmail.com"
        if not re.fullmatch(r"[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?", imap_host):
            raise ValueError("CVBOOST_IMAP_HOST must be a hostname")
        hosts = tuple(h.strip() for h in _env("CVBOOST_WEB_HOSTS", "").split(",") if h.strip())
        return cls(
            kits_override=Path(kits) if kits else None,
            panel_url=panel or None,
            imap_user=imap_user,
            imap_password=imap_password,
            imap_host=imap_host,
            mail_forwarders=tuple(
                a.strip().lower()
                for a in _env("CVBOOST_MAIL_FORWARDERS", "").split(",")
                if a.strip()
            ),
            imap_mailbox=_env("CVBOOST_IMAP_MAILBOX") or "INBOX",
            web_user=user,
            web_password=password,
            web_hosts=hosts or ("localhost", "127.0.0.1"),
            web_secret=_env("CVBOOST_WEB_SECRET") or None,
            web_trust_network=_env("CVBOOST_WEB_TRUST_NETWORK") == "1",
            db_path=Path(_env("CVBOOST_DB", "data/cvboost.sqlite")),
            log_level=_env("CVBOOST_LOG_LEVEL", "INFO").upper(),
            rules_path=Path(_env("CVBOOST_RULES", "config/rules.yaml")),
            llm_config_path=Path(_env("CVBOOST_LLM_CONFIG", "config/llm.yaml")),
            worker_config_path=Path(_env("CVBOOST_WORKER_CONFIG", "config/worker.yaml")),
            telegram_token=_env("TELEGRAM_BOT_TOKEN") or None,
            telegram_chat_id=chat_id,
            resume_dir=Path(_env("CVBOOST_RESUME_DIR", "config/resume")),
            profile_path=Path(_env("CVBOOST_PROFILE", "config/profile.yaml")),
            companies_path=Path(_env("CVBOOST_COMPANIES", "config/companies.yaml")),
            outreach_path=Path(_env("CVBOOST_OUTREACH", "config/outreach.yaml")),
        )
