import asyncio
import json
from dataclasses import replace
from urllib.parse import parse_qs

import httpx
import pytest

from studio.autopay import AutoPay
from studio.config import Config
from studio.store import Store, RuleError


@pytest.fixture
def setup(tmp_path):
    s = Store(tmp_path / "test.db")
    s.user(1)
    cfg = Config(
        "123:fake",
        99,
        crypto_pay_token="fake-app-token",
        yoomoney_wallet="410011111",
        yoomoney_token="fake-readonly-token",
    )
    return s, cfg


def offer(s, provider, currency, amount):
    s.set_setting(
        "method:auto", json.dumps({"currency": currency, "provider": provider, "instructions": "API"})
    )
    oid = s.create_order(1, "classic_head", "brief", [])
    s.external_offer(oid, "auto", amount, 180, "week")
    return oid


def test_crypto_api_creation_validation_idempotent_confirmation(setup):
    s, cfg = setup
    oid = offer(s, "cryptopay", "TON", "2.5")
    remote = {}

    def api(request):
        if request.url.path.endswith("createInvoice"):
            remote.update(
                json.loads(request.content),
                invoice_id=10,
                bot_invoice_url="https://t.me/CryptoBot?start=invoice",
                status="active",
            )
            return httpx.Response(200, json={"ok": True, "result": remote})
        return httpx.Response(200, json={"ok": True, "result": {"items": [remote]}})

    auto = AutoPay(s, cfg, httpx.MockTransport(api))
    invoice = asyncio.run(auto.prepare(oid, "https://t.me/test"))
    assert invoice["remote_id"] == "10"
    with pytest.raises(RuleError):
        s.cancel_order(1, oid)
    assert asyncio.run(auto.check(oid)) is False
    remote["status"] = "paid"
    assert asyncio.run(auto.check(oid)) is True
    assert asyncio.run(auto.check(oid)) is False
    assert s.order(oid)["status"] == "paid"
    assert len(s.rows("SELECT * FROM external_payments")) == 1


@pytest.mark.parametrize("field,value", [("amount", "0.01"), ("asset", "USDT"), ("payload", "wrong")])
def test_crypto_cannot_credit_mismatched_invoice(setup, field, value):
    s, cfg = setup
    oid = offer(s, "cryptopay", "TON", "2.5")
    remote = {}

    def api(request):
        if request.url.path.endswith("createInvoice"):
            remote.update(
                json.loads(request.content),
                invoice_id=10,
                bot_invoice_url="https://t.me/CryptoBot?start=invoice",
                status="active",
            )
            return httpx.Response(200, json={"ok": True, "result": remote})
        return httpx.Response(200, json={"ok": True, "result": {"items": [remote]}})

    auto = AutoPay(s, cfg, httpx.MockTransport(api))
    asyncio.run(auto.prepare(oid, "https://t.me/test"))
    remote.update(status="paid")
    remote[field] = value
    with pytest.raises(RuleError):
        asyncio.run(auto.check(oid))
    assert s.order(oid)["status"] == "quoted"


def test_missing_credentials_never_create_payable_invoice(setup):
    s, cfg = setup
    oid = offer(s, "cryptopay", "USDT", "3")
    auto = AutoPay(s, replace(cfg, crypto_pay_token=""))
    with pytest.raises(RuleError):
        asyncio.run(auto.prepare(oid, "https://t.me/test"))
    assert s.auto_invoice(oid) is None


def test_uncertain_crypto_creation_recovers_without_double_post(setup):
    s, cfg = setup
    oid = offer(s, "cryptopay", "USDT", "3")
    remote = {}
    created = 0

    def api(request):
        nonlocal created
        if request.url.path.endswith("createInvoice"):
            created += 1
            remote.update(
                json.loads(request.content),
                invoice_id=10,
                bot_invoice_url="https://t.me/CryptoBot?start=invoice",
                status="active",
            )
            raise httpx.ReadTimeout("timeout", request=request)
        return httpx.Response(200, json={"ok": True, "result": {"items": [remote]}})

    auto = AutoPay(s, cfg, httpx.MockTransport(api))
    with pytest.raises(httpx.ReadTimeout):
        asyncio.run(auto.prepare(oid, "https://t.me/test"))
    assert s.auto_invoice(oid)["state"] == "uncertain"
    assert asyncio.run(auto.prepare(oid, "https://t.me/test"))["remote_id"] == "10"
    assert created == 1


def test_yoomoney_uses_exact_wallet_label_final_status_and_net_amount(setup):
    s, cfg = setup
    oid = offer(s, "yoomoney", "RUB", "180")
    operation = {"operation_id": "op1", "direction": "in", "status": "success", "amount": "180"}

    def api(request):
        if request.url.path.endswith("account-info"):
            return httpx.Response(200, json={"account": cfg.yoomoney_wallet})
        if request.url.path.endswith("quickpay/confirm"):
            fields = parse_qs(request.content.decode())
            operation["label"] = fields["label"][0]
            assert fields["sum"][0] == "185.57"
            return httpx.Response(302, headers={"Location": "https://yoomoney.ru/transfer/confirm?invoice=1"})
        return httpx.Response(200, json={"operations": [operation]})

    auto = AutoPay(s, cfg, httpx.MockTransport(api))
    asyncio.run(auto.prepare(oid, "https://t.me/test"))
    operation["amount"] = "179.99"
    assert asyncio.run(auto.check(oid)) is False
    operation.update(amount="180", status="in_progress")
    assert asyncio.run(auto.check(oid)) is False
    operation.update(status="success", direction="out")
    assert asyncio.run(auto.check(oid)) is False
    operation.update(direction="in", label="wrong")
    assert asyncio.run(auto.check(oid)) is False
    operation["label"] = s.auto_invoice(oid)["nonce"]
    assert asyncio.run(auto.check(oid)) is True


def test_wrong_yoomoney_account_rejected(setup):
    s, cfg = setup
    oid = offer(s, "yoomoney", "RUB", "180")
    auto = AutoPay(
        s, cfg, httpx.MockTransport(lambda request: httpx.Response(200, json={"account": "other"}))
    )
    with pytest.raises(RuleError):
        asyncio.run(auto.prepare(oid, "https://t.me/test"))


def test_late_auto_payment_after_cancel_goes_to_review_without_rebooking(setup):
    s, cfg = setup
    oid = offer(s, "cryptopay", "TON", "2.5")
    invoice, fresh = s.reserve_auto(oid)
    s.attach_auto(invoice["id"], "10", "https://t.me/CryptoBot?start=invoice")
    s.void_auto(oid)
    s.cancel_order(99, oid, admin=True)
    assert s.confirm_auto(invoice["id"], "10") == "review"
    assert s.order(oid)["status"] == "cancelled"
    assert len(s.free_slots("classic_head")) == 3
    assert len(s.rows("SELECT * FROM auto_review")) == 1


def test_expired_crypto_invoice_can_release_slot(setup):
    s, cfg = setup
    oid = offer(s, "cryptopay", "TON", "2.5")
    invoice, fresh = s.reserve_auto(oid)
    s.attach_auto(invoice["id"], "10", "https://t.me/CryptoBot?start=invoice")
    s.auto_state(invoice["id"], "expired")
    s.cancel_order(1, oid)
    assert len(s.free_slots("classic_head")) == 3
