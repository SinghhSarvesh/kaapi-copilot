"""
Payment provider factory — selects Mock or Razorpay based on settings.payment_mode.
The rest of the application only ever imports `get_payment_provider`.
"""
from app.core.config import settings
from app.providers.payment.mock import mock_payment_provider


def get_payment_provider():
    if settings.payment_mode == "razorpay":
        from app.providers.payment.razorpay_provider import RazorpayPaymentProvider
        return RazorpayPaymentProvider(settings.razorpay_key_id, settings.razorpay_key_secret)
    return mock_payment_provider
