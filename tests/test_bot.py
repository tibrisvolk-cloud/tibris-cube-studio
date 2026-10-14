import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from studio.bot import Bot
from studio.config import Config
from studio.store import RuleError, Store


@pytest.fixture
def bot(tmp_path):
    return Bot(Config("123456:test_token", 99, db_path=str(tmp_path / "bot.db")))


def ctx():
    return NS(
        bot=NS(
            send_message=AsyncMock(),
            send_photo=AsyncMock(),
            send_document=AsyncMock(),
            copy_message=AsyncMock(),
            send_invoice=AsyncMock(),
            refund_star_payment=AsyncMock(),
        )
    )


def message(uid, text=None, photo=None, document=None):
    m = NS(
        text=text, photo=photo or [], document=document, chat_id=uid, message_id=10, reply_text=AsyncMock()
    )
    return NS(effective_user=NS(id=uid, username="test"), effective_message=m, message=m)


def callback(uid, data):
    m = NS(reply_text=AsyncMock())
    q = NS(data=data, from_user=NS(id=uid, username="test"), answer=AsyncMock(), message=m)
    return NS(callback_query=q, effective_message=m, effective_user=q.from_user)


def test_photo_broadcast_routes_to_confirmation(bot):
    bot.s.save_dialog(99, "broadcast", {})
    update = message(99, photo=[NS(file_id="image")])
    asyncio.run(bot.on_message(update, ctx()))
    assert bot.s.dialog(99)[0] == "broadcast_confirm"


def test_admin_upload_document_as_cover(bot):
    bot.s.save_dialog(99, "image", {"key": "main"})
    document = NS(file_id="original_png", file_size=1024, file_name="cover.png", mime_type="image/png")
    asyncio.run(bot.on_message(message(99, document=document), ctx()))
    assert bot.s.one("SELECT * FROM media WHERE key='main'")["kind"] == "document"
    assert bot.s.dialog(99) is None


def test_user_cannot_upload_admin_cover(bot):
    bot.s.save_dialog(1, "image", {"key": "main"})
    with pytest.raises(RuleError):
        asyncio.run(bot.on_message(message(1, photo=[NS(file_id="x")]), ctx()))
    assert bot.s.one("SELECT * FROM media") is None


def test_external_task_has_timer_button(bot):
    tid = bot.s.add_task("https://example.com", "visit", 5, "external", delay=30)
    context = ctx()
    asyncio.run(bot.callback(callback(1, f"task:{tid}"), context))
    markup = context.bot.send_message.call_args.kwargs["reply_markup"]
    assert any(b.callback_data == f"taskstart:{tid}" for row in markup.inline_keyboard for b in row)
    assert any(b.url == "https://example.com" for row in markup.inline_keyboard for b in row)


def test_navigation_clears_stale_answer(bot):
    bot.s.save_dialog(1, "answer", {"rid": 1})
    asyncio.run(bot.callback(callback(1, "studio"), ctx()))
    assert bot.s.dialog(1) is None


def test_order_refs_submission_and_no_duplicate(bot):
    context = ctx()
    asyncio.run(bot.callback(callback(1, "neworder:classic_head"), context))
    asyncio.run(bot.callback(callback(1, "build_bg:0"), context))
    asyncio.run(bot.callback(callback(1, "build_second:0"), context))
    asyncio.run(bot.on_message(message(1, "My OC head"), context))
    asyncio.run(bot.on_message(message(1, photo=[NS(file_id="ref")]), context))
    asyncio.run(bot.callback(callback(1, "refs_done"), context))
    asyncio.run(bot.callback(callback(1, "build_pay:rub"), context))
    asyncio.run(bot.callback(callback(1, "build_submit"), context))
    assert len(bot.s.rows("SELECT * FROM orders")) == 1
    with pytest.raises(RuleError):
        asyncio.run(bot.callback(callback(1, "build_submit"), context))


def test_invoice_ownership_and_xtr(bot):
    bot.s.user(1)
    oid = bot.s.create_order(1, "classic_head", "brief", [])
    bot.s.quote(oid, 180, 250, "week")
    with pytest.raises(RuleError):
        asyncio.run(bot.callback(callback(2, f"invoice:{oid}"), ctx()))
    context = ctx()
    asyncio.run(bot.callback(callback(1, f"invoice:{oid}"), context))
    args = context.bot.send_invoice.call_args.args
    assert args[4] == "XTR"
    assert args[5][0].amount == 250
    assert context.bot.send_invoice.call_args.kwargs["provider_token"] == ""


def test_failed_delivery_leaves_order_retryable(bot):
    bot.s.user(1)
    oid = bot.s.create_order(1, "classic_head", "brief", [])
    o = bot.s.quote(oid, 180, 250, "week")
    bot.s.paid(1, o["invoice"], 250, "XTR", "charge")
    bot.s.save_dialog(99, "deliver", {"oid": oid})
    context = ctx()
    from telegram.error import Forbidden

    context.bot.send_photo.side_effect = Forbidden("blocked")
    with pytest.raises(Forbidden):
        asyncio.run(bot.on_message(message(99, photo=[NS(file_id="result")]), context))
    assert bot.s.order(oid)["status"] == "paid"
    assert bot.s.dialog(99)[0] == "deliver"


def test_application_builds_with_real_pinned_library(bot):
    app = bot.application()
    assert app.bot.token == "123456:test_token"
    assert app.concurrent_updates == 1
    asyncio.run(app.shutdown())


def test_restart_keeps_uploaded_images(bot):
    bot.s.set_media("classic", "file", "photo")
    reopened = Store(bot.s.path)
    assert reopened.one("SELECT file_id FROM media WHERE key='classic'")["file_id"] == "file"


def test_profile_has_balance_orders_and_direct_artist_chat(bot):
    bot.s.set_setting("artist_username", "TibrisExample")
    context = ctx()
    asyncio.run(bot.callback(callback(1, "profile"), context))
    call = context.bot.send_message.call_args
    assert "Баланс Cube: 0" in call.args[1]
    markup = call.kwargs["reply_markup"]
    assert any(b.url == "https://t.me/TibrisExample" for row in markup.inline_keyboard for b in row)
    assert any(b.callback_data == "orders:0" for row in markup.inline_keyboard for b in row)


def test_ych_menu_and_legacy_button_are_removed(bot):
    context = ctx()
    asyncio.run(bot.callback(callback(1, "studio"), context))
    markup = context.bot.send_photo.call_args.kwargs["reply_markup"]
    assert not any(b.callback_data == "cat:ych" for row in markup.inline_keyboard for b in row)
    with pytest.raises(RuleError):
        asyncio.run(bot.callback(callback(99, "addych"), context))


def test_user_promo_router_creates_discount_coupon(bot):
    bot.s.add_promo("TIBRIS10", 10, 1)
    asyncio.run(bot.callback(callback(1, "promo"), ctx()))
    asyncio.run(bot.on_message(message(1, "tibris10"), ctx()))
    assert bot.s.one("SELECT percent FROM coupons WHERE user_id=1")["percent"] == 10
