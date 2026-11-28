"""The adapter's user-facing strings, in every language Hermes core shipped them in.

Core carried this key in its bundled ``locales/*.yaml`` until Weixin left core; the plugin keeps the same
text and the same language choice (``HERMES_LANGUAGE`` from the profile's secrets, else
``display.language`` from its config.yaml, else English) without importing core's private i18n module.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

_MESSAGES: dict[str, dict[str, str]] = {
    'af': {'platform.weixin.voice_as_attachment': "[stemboodskap as aanhegsel]"},
    'ar': {'platform.weixin.voice_as_attachment': "[رسالة صوتية كمرفق]"},
    'de': {'platform.weixin.voice_as_attachment': "[Sprachnachricht als Anhang]"},
    'en': {'platform.weixin.voice_as_attachment': "[voice message as attachment]"},
    'es': {'platform.weixin.voice_as_attachment': "[mensaje de voz como adjunto]"},
    'fr': {'platform.weixin.voice_as_attachment': "[message vocal en pièce jointe]"},
    'ga': {'platform.weixin.voice_as_attachment': "[teachtaireacht ghutha mar cheangaltán]"},
    'hu': {'platform.weixin.voice_as_attachment': "[hangüzenet mellékletként]"},
    'it': {'platform.weixin.voice_as_attachment': "[messaggio vocale come allegato]"},
    'ja': {'platform.weixin.voice_as_attachment': "[音声メッセージを添付ファイルとして送信]"},
    'ko': {'platform.weixin.voice_as_attachment': "[음성 메시지 첨부]"},
    'pt': {'platform.weixin.voice_as_attachment': "[mensagem de voz como anexo]"},
    'ru': {'platform.weixin.voice_as_attachment': "[голосовое сообщение во вложении]"},
    'tr': {'platform.weixin.voice_as_attachment': "[ek olarak sesli mesaj]"},
    'uk': {'platform.weixin.voice_as_attachment': "[голосове повідомлення як вкладення]"},
    'zh-hant': {'platform.weixin.voice_as_attachment': "[語音訊息作為附件]"},
    'zh': {'platform.weixin.voice_as_attachment': "[语音消息作为附件]"},
}

# Spellings core's language picker accepted for these packs (``agent.i18n._LANGUAGE_ALIASES``).
_ALIASES = {
    "chinese": "zh", "mandarin": "zh", "zh-cn": "zh", "zh-hans": "zh", "zh-sg": "zh",
    "traditional-chinese": "zh-hant", "traditional_chinese": "zh-hant",
    "zh-tw": "zh-hant", "zh-hk": "zh-hant", "zh-mo": "zh-hant",
}


def _language_setting() -> str:
    from agent.secret_scope import UnscopedSecretError, get_secret
    try:
        value = get_secret("HERMES_LANGUAGE")
    except UnscopedSecretError:  # unscoped default-profile path: the process env is that profile's own value
        value = os.environ.get("HERMES_LANGUAGE")
    if value:
        return str(value)
    try:
        from hermes_cli.config import load_config_readonly
        return str((load_config_readonly().get("display") or {}).get("language") or "")
    except Exception:  # an unreadable config.yaml must never break a reply; core's t() degrades the same way
        logger.debug("Could not read display.language", exc_info=True)
        return ""


def language() -> str:
    """The active profile's language id among the ones this module carries, else ``en``."""
    key = _language_setting().strip().lower().replace("_", "-")
    key = _ALIASES.get(key, key)
    if key in _MESSAGES:
        return key
    base = key.split("-", 1)[0]
    return base if base in _MESSAGES else "en"


def t(key: str, **format_kwargs) -> str:
    """``key`` in the active language (English fallback, then the bare key), ``str.format``-ed."""
    value = _MESSAGES.get(language(), {}).get(key) or _MESSAGES["en"].get(key, key)
    if not format_kwargs:
        return value
    try:
        return value.format(**format_kwargs)
    except (KeyError, IndexError, ValueError):
        return value
