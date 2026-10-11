"""Version of the privacy policy that people accept.

Change this value together with app/static/privacy.html and the data-policy-version
attribute in app/static/index.html. A test keeps the three in step. A new version asks
existing users to accept it again on their next visit.
"""

from fastapi import HTTPException

POLICY_VERSION = "2026-10-12"


def require_consent(accepted, version):
    """Registration and Telegram sign-in both need an explicit, current acceptance."""
    if accepted is not True:
        raise HTTPException(422, "Чтобы продолжить, дайте согласие на обработку персональных данных")
    if version != POLICY_VERSION:
        raise HTTPException(422, "Политика обработки данных обновилась. Обновите страницу и подтвердите согласие ещё раз")
