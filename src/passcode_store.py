import logging

import keyring

logger = logging.getLogger(__name__)

_SERVICE = "iPhoneMirror"
_ACCOUNT_PREFIX = "passcode"


def _account(udid: str) -> str:
    return f"{_ACCOUNT_PREFIX}:{udid}"


def get_passcode(udid: str) -> str | None:
    if not udid:
        return None
    try:
        return keyring.get_password(_SERVICE, _account(udid))
    except Exception as e:
        logger.warning("Keychain read failed: %s", e)
        return None


def set_passcode(udid: str, passcode: str) -> bool:
    if not udid:
        return False
    try:
        if passcode:
            keyring.set_password(_SERVICE, _account(udid), passcode)
        else:
            clear_passcode(udid)
        return True
    except Exception as e:
        logger.warning("Keychain write failed: %s", e)
        return False


def clear_passcode(udid: str) -> None:
    if not udid:
        return
    try:
        keyring.delete_password(_SERVICE, _account(udid))
    except Exception:
        pass
