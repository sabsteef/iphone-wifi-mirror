import logging
import secrets

import keyring

logger = logging.getLogger(__name__)

_SERVICE = "iPhoneMirror"
_ACCOUNT = "wda_auth_token"


def get_or_create_token() -> str:
    try:
        token = keyring.get_password(_SERVICE, _ACCOUNT)
        if token:
            return token
    except Exception as e:
        logger.warning("Keychain read failed: %s", e)

    token = secrets.token_urlsafe(32)
    try:
        keyring.set_password(_SERVICE, _ACCOUNT, token)
        logger.info("New WDA auth token generated and stored")
    except Exception as e:
        logger.warning("Keychain write failed, using in-memory token: %s", e)
    return token


def clear_token() -> None:
    try:
        keyring.delete_password(_SERVICE, _ACCOUNT)
    except Exception:
        pass
