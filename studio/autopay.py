"""Provider-backed automatic confirmation. Never accepts client receipts as payment evidence."""

import asyncio
import logging
from decimal import Decimal, ROUND_UP
from urllib.parse import urlparse

import httpx

from .store import RuleError

log = logging.getLogger(__name__)


class AutoPay:
    def __init__(self, store, config, transport=None):
        self.s, self.cfg, self.transport = store, config, transport
        self.lock = asyncio.Lock()

    def client(self):
        return httpx.AsyncClient(transport=self.transport, timeout=20, follow_redirects=False)

    async def crypto(self, client, method, data):
        if not self.cfg.crypto_pay_token:
            raise RuleError("Crypto Pay пока не подключён. Tibris должен добавить ключ в .env.")
        base = (
            "https://testnet-pay.crypt.bot/api/"
            if self.cfg.crypto_pay_testnet
            else "https://pay.crypt.bot/api/"
        )
        r = await client.post(
            base + method, json=data, headers={"Crypto-Pay-API-Token": self.cfg.crypto_pay_token}
        )
        r.raise_for_status()
        body = r.json()
        if body.get("ok") is not True:
            raise RuleError("Платёжный сервис отклонил запрос. Tibris проверит настройки.")
        return body["result"]

    async def money(self, client, method, data=None):
        if not self.cfg.yoomoney_token or not self.cfg.yoomoney_wallet:
            raise RuleError(
                "ЮMoney пока не подключён. Tibris должен добавить кошелёк и read-only токен в .env."
            )
        r = await client.post(
            "https://yoomoney.ru/api/" + method,
            data=data or {},
            headers={"Authorization": "Bearer " + self.cfg.yoomoney_token},
        )
        r.raise_for_status()
        body = r.json()
        if body.get("error"):
            raise RuleError("Не удалось проверить кошелёк. Tibris должен проверить права токена.")
        return body

    async def verify_wallet(self, client):
        body = await self.money(client, "account-info")
        if str(body.get("account")) != self.cfg.yoomoney_wallet:
            raise RuleError("Номер кошелька и read-only токен принадлежат разным счетам.")

    async def prepare(self, oid, return_url):
        async with self.lock:
            order = self.s.order(oid)
            provider = order["gateway"]
            if provider == "cryptopay" and not self.cfg.crypto_pay_token:
                raise RuleError("Crypto Pay пока не подключён.")
            if provider == "yoomoney" and (not self.cfg.yoomoney_token or not self.cfg.yoomoney_wallet):
                raise RuleError("ЮMoney пока не подключён.")
            invoice, fresh = self.s.reserve_auto(oid)
            if invoice["state"] == "pending":
                return invoice
            if invoice["state"] == "paid":
                raise RuleError("Заказ уже оплачен.")
            if invoice["state"] in {"expired", "watching", "review"}:
                raise RuleError("Счёт истёк. Отмените заявку и оформите новую.")
            async with self.client() as client:
                try:
                    if provider == "cryptopay":
                        # Crypto Pay currently names the native asset TON, regardless of display branding.
                        asset = "TON" if invoice["currency"] == "GRAM" else invoice["currency"]
                        if asset not in {"TON", "USDT"}:
                            raise RuleError("Crypto Pay: доступны TON и USDT.")
                        if fresh:
                            result = await self.crypto(
                                client,
                                "createInvoice",
                                {
                                    "currency_type": "crypto",
                                    "asset": asset,
                                    "amount": invoice["amount"],
                                    "description": f"Tibris order #{oid}",
                                    "payload": invoice["nonce"],
                                    "expires_in": 3600,
                                    "allow_anonymous": False,
                                    "allow_comments": False,
                                },
                            )
                        else:
                            # A timed-out creation must never blindly create a second crypto invoice.
                            result = None
                            offset = 0
                            while offset < 10000:
                                page = await self.crypto(
                                    client, "getInvoices", {"count": 1000, "offset": offset}
                                )
                                items = page.get("items", [])
                                result = next(
                                    (v for v in items if v.get("payload") == invoice["nonce"]), None
                                )
                                if result or len(items) < 1000:
                                    break
                                offset += 1000
                            if not result:
                                raise RuleError(
                                    "Создание счёта не подтверждено. Tibris проверит его через /autopaystatus; повторный счёт не создаётся."
                                )
                        self._validate_crypto(invoice, result)
                        self.s.attach_auto(
                            invoice["id"],
                            result["invoice_id"],
                            result.get("bot_invoice_url") or result.get("pay_url", ""),
                        )
                    elif provider == "yoomoney":
                        if invoice["currency"] != "RUB":
                            raise RuleError("ЮMoney принимает этот заказ в RUB.")
                        await self.verify_wallet(client)
                        fee = Decimal(self.cfg.yoomoney_card_fee)
                        if not 0 <= fee < 1:
                            raise RuleError("Проверьте YOOMONEY_CARD_FEE.")
                        # Quote is net to artist; display the exact card total before redirecting.
                        gross = (Decimal(invoice["amount"]) / (1 - fee)).quantize(
                            Decimal("0.01"), rounding=ROUND_UP
                        )
                        r = await client.post(
                            "https://yoomoney.ru/quickpay/confirm",
                            data={
                                "receiver": self.cfg.yoomoney_wallet,
                                "quickpay-form": "button",
                                "sum": str(gross),
                                "label": invoice["nonce"],
                                "paymentType": "AC",
                                "successURL": return_url,
                            },
                        )
                        if r.status_code not in {301, 302, 303, 307, 308}:
                            raise RuleError(
                                "ЮMoney не вернул платёжную ссылку. Tibris проверит доступность формы."
                            )
                        url = r.headers.get("location", "")
                        if not url.startswith("https://") or urlparse(url).hostname not in {
                            "yoomoney.ru",
                            "www.yoomoney.ru",
                        }:
                            raise RuleError("Получена неожиданная ссылка платёжного сервиса.")
                        self.s.attach_auto(invoice["id"], invoice["nonce"], url, str(gross))
                    else:
                        raise RuleError("Неизвестный автоматический провайдер.")
                except (httpx.HTTPError, RuleError, KeyError, ValueError):
                    self.s.auto_state(invoice["id"], "uncertain")
                    raise
            return self.s.auto_invoice(oid)

    def _validate_crypto(self, invoice, remote):
        asset = "TON" if invoice["currency"] == "GRAM" else invoice["currency"]
        if (
            remote.get("payload") != invoice["nonce"]
            or remote.get("currency_type") != "crypto"
            or remote.get("asset") != asset
            or Decimal(str(remote.get("amount", "0"))) != Decimal(invoice["amount"])
        ):
            raise RuleError("Данные счёта провайдера не совпали с заказом.")

    async def check(self, oid):
        async with self.lock:
            invoice = self.s.auto_invoice(oid)
            if not invoice or invoice["state"] not in {"pending", "paid", "watching"}:
                return False
            if invoice["state"] == "paid":
                return False
            async with self.client() as client:
                if invoice["provider"] == "cryptopay":
                    if not invoice["remote_id"]:
                        return False
                    result = await self.crypto(client, "getInvoices", {"invoice_ids": invoice["remote_id"]})
                    remote = next(
                        (
                            v
                            for v in result.get("items", [])
                            if str(v.get("invoice_id")) == invoice["remote_id"]
                        ),
                        None,
                    )
                    if not remote:
                        return False
                    self._validate_crypto(invoice, remote)
                    if remote["status"] == "expired":
                        self.s.auto_state(invoice["id"], "expired")
                        return False
                    if remote["status"] != "paid":
                        return False
                    reference = str(remote["invoice_id"])
                else:
                    await self.verify_wallet(client)
                    # Filter by secret label and inspect every page. Incoming funds must be final and accepted.
                    data = {
                        "type": "deposition",
                        "label": invoice["nonce"],
                        "records": "100",
                        "details": "true",
                    }
                    reference = None
                    while True:
                        page = await self.money(client, "operation-history", data)
                        for remote in page.get("operations", []):
                            if (
                                remote.get("label") == invoice["nonce"]
                                and remote.get("direction") == "in"
                                and remote.get("status") == "success"
                                and not remote.get("codepro")
                                and not remote.get("unaccepted")
                                and Decimal(str(remote.get("amount", "0"))) >= Decimal(invoice["amount"])
                            ):
                                reference = str(remote["operation_id"])
                                break
                        if reference or not page.get("next_record"):
                            break
                        next_record = str(page["next_record"])
                        if data.get("start_record") == next_record:
                            raise RuleError("Провайдер вернул некорректную страницу истории.")
                        data["start_record"] = next_record
                    if not reference:
                        return False
                return self.s.confirm_auto(invoice["id"], reference)
