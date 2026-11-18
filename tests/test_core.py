"""
Pure-logic tests (no Telegram / database needed):
    python -m unittest discover -s tests -v        (or:  pytest)
"""
import os
import sys
import unittest
from decimal import Decimal

os.environ.setdefault("BOT_TOKEN", "123:TEST")
os.environ["SECRET_ENCRYPTION_KEY"] = "unit-test-key"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import normalize_database_url  # noqa: E402
from utils import crypto  # noqa: E402
from utils.helpers import (  # noqa: E402
    esc, fmt_money, normalize_txn_id, parse_amount, parse_phone, parse_uid, to_money,
)


class ParseAmountTests(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(parse_amount("500"), Decimal("500.00"))
        self.assertEqual(parse_amount(" 1,250.5 "), Decimal("1250.50"))
        self.assertEqual(parse_amount("৫০০"), Decimal("500.00"))      # Bengali digits

    def test_nan_inf_rejected(self):
        # float("nan") / float("inf") used to slip through `amount <= 0` checks
        for bad in ("nan", "NaN", "inf", "-inf", "Infinity", "1e999999"):
            self.assertIsNone(parse_amount(bad), bad)

    def test_bounds_and_garbage(self):
        self.assertIsNone(parse_amount("0"))
        self.assertIsNone(parse_amount("-5"))
        self.assertIsNone(parse_amount("abc"))
        self.assertIsNone(parse_amount(""))
        self.assertIsNone(parse_amount(None))
        self.assertIsNone(parse_amount("5", min_value=Decimal("10")))
        self.assertIsNone(parse_amount("60000", max_value=Decimal("50000")))
        self.assertEqual(parse_amount("10", min_value=Decimal("10")), Decimal("10.00"))

    def test_rounding_is_decimal_exact(self):
        self.assertEqual(parse_amount("0.1") + parse_amount("0.2"), Decimal("0.30"))   # float gives 0.30000000000000004
        self.assertEqual(parse_amount("10.005"), Decimal("10.01"))


class ValidatorTests(unittest.TestCase):
    def test_uid(self):
        self.assertEqual(parse_uid("123456789"), "123456789")
        self.assertEqual(parse_uid(" ১২৩৪৫৬৭৮৯ "), "123456789")
        for bad in ("12345", "1" * 15, "12a456789", "²²²²²²²²", "", None):
            self.assertIsNone(parse_uid(bad), bad)

    def test_txn_id(self):
        self.assertEqual(normalize_txn_id(" 8n7a 6d5e4c "), "8N7A6D5E4C")
        self.assertIsNone(normalize_txn_id("abc"))
        self.assertIsNone(normalize_txn_id("<script>alert(1)</script>"))

    def test_phone(self):
        self.assertEqual(parse_phone("017-1234 5678"), "01712345678")
        self.assertEqual(parse_phone("+8801712345678"), "+8801712345678")
        self.assertIsNone(parse_phone("12345"))
        self.assertIsNone(parse_phone("hello"))


class FormattingTests(unittest.TestCase):
    def test_escape(self):
        self.assertEqual(esc("<b>x</b> & y"), "&lt;b&gt;x&lt;/b&gt; &amp; y")
        self.assertEqual(esc(None), "")
        self.assertEqual(esc(123), "123")

    def test_money(self):
        self.assertEqual(to_money(95), Decimal("95.00"))
        self.assertEqual(to_money(0.1 + 0.2), Decimal("0.30"))
        self.assertEqual(fmt_money(1234.5), "৳1,234.50")
        self.assertEqual(fmt_money(None), "৳0.00")


class CryptoTests(unittest.TestCase):
    def test_roundtrip(self):
        token = crypto.encrypt("super-secret-api-key")
        self.assertTrue(token.startswith(crypto.PREFIX))
        self.assertNotIn("super-secret", token)
        self.assertEqual(crypto.decrypt(token), "super-secret-api-key")

    def test_idempotent_and_legacy(self):
        token = crypto.encrypt("abc")
        self.assertEqual(crypto.encrypt(token), token)           # never double-encrypts
        self.assertEqual(crypto.decrypt("plain-old-value"), "plain-old-value")  # legacy plaintext still readable
        self.assertIsNone(crypto.encrypt(None))
        self.assertIsNone(crypto.decrypt(None))

    def test_wrong_key_is_detected(self):
        token = crypto.encrypt("abc")
        import base64
        import hashlib
        from cryptography.fernet import Fernet
        original = crypto._fernet
        try:
            crypto._fernet = Fernet(base64.urlsafe_b64encode(hashlib.sha256(b"other").digest()))
            with self.assertRaises(RuntimeError):
                crypto.decrypt(token)
        finally:
            crypto._fernet = original


class DatabaseUrlTests(unittest.TestCase):
    def test_render_style(self):
        self.assertEqual(
            normalize_database_url("postgresql://u:p@dpg-abc/db"),
            "postgresql+asyncpg://u:p@dpg-abc/db",
        )
        self.assertEqual(
            normalize_database_url("postgres://u:p@dpg-abc/db"),
            "postgresql+asyncpg://u:p@dpg-abc/db",
        )

    def test_neon_style(self):
        out = normalize_database_url("postgresql://u:p@ep-x-pooler.neon.tech/db?sslmode=require&channel_binding=require")
        self.assertTrue(out.startswith("postgresql+asyncpg://"))
        self.assertIn("ssl=require", out)
        self.assertNotIn("sslmode", out)
        self.assertNotIn("channel_binding", out)

    def test_untouched(self):
        self.assertEqual(normalize_database_url("sqlite+aiosqlite:///./bot.db"), "sqlite+aiosqlite:///./bot.db")
        self.assertEqual(normalize_database_url("postgresql+asyncpg://u:p@h/db"), "postgresql+asyncpg://u:p@h/db")


if __name__ == "__main__":
    unittest.main()
