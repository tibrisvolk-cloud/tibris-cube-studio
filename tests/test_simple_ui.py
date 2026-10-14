import asyncio
import json

import pytest
from telegram.error import BadRequest

from studio.ordering import total, method_amount
from studio.store import Store, RuleError
from studio.bot import Bot, text_chunks
from studio.config import Config
from test_bot import ctx, message, callback


@pytest.fixture
def bot(tmp_path):
    return Bot(Config("123456:test_token", 99, db_path=str(tmp_path / "bot.db")))


def run(coroutine):
    return asyncio.run(coroutine)


def press(bot, context, data, uid=1):
    run(bot.callback(callback(uid, data), context))


def last_card(context):
    if context.bot.send_message.call_args:
        return context.bot.send_message.call_args
    return context.bot.send_photo.call_args


def test_menu_image_contains_caption_and_keyboard(bot):
    context = ctx()
    press(bot, context, "cat:classic")
    photo = context.bot.send_photo.call_args
    assert "Classic" in photo.kwargs["caption"]
    assert context.bot.send_message.call_count == 0
    buttons = photo.kwargs["reply_markup"].inline_keyboard
    assert any(b.text == "Фулл · 600 ₽" for row in buttons for b in row)


def test_uploaded_file_has_text_and_buttons(bot):
    context = ctx()
    bot.s.set_media("main", "file", "document")
    run(bot.home(context, 1))
    assert context.bot.send_document.call_args.kwargs["caption"]
    assert context.bot.send_document.call_args.kwargs["reply_markup"]
    assert context.bot.send_message.call_count == 0


def test_failed_image_falls_back_to_complete_text(bot):
    context = ctx()
    context.bot.send_photo.side_effect = BadRequest("invalid file")
    bot.s.set_media("main", "invalid")
    run(bot.home(context, 1))
    assert "Digital Cube" in context.bot.send_message.call_args.args[1]
    assert context.bot.send_message.call_args.kwargs["reply_markup"]


def test_long_card_retains_all_text_and_final_buttons(bot):
    context = ctx()
    text = "🐾" * 1100 + " конец"
    run(bot.tell(context, 1, text, [[("Домой", "home")]], "main"))
    caption = context.bot.send_photo.call_args.kwargs["caption"]
    assert len(caption.encode("utf-16-le")) // 2 <= 1024
    assert caption + "".join(c.args[1] for c in context.bot.send_message.call_args_list) == text
    assert context.bot.send_message.call_args.kwargs["reply_markup"]


def test_full_background_three_characters_coupon_and_acceptance(bot):
    context = ctx()
    bot.s.add_promo("TIBRIS10", 10)
    bot.s.user(1)
    cid, _ = bot.s.claim_promo(1, "TIBRIS10")
    bot.s.set_setting(
        "method:sbp", json.dumps({"currency": "RUB", "label": "СБП", "instructions": "bank details"})
    )
    press(bot, context, "product:classic_full")
    assert bot.s.dialog(1)[1]["step"] == "bg"
    press(bot, context, "build_bg:1")
    press(bot, context, "build_second:1")
    press(bot, context, "build_third:1")
    run(bot.on_message(message(1, "Three foxes"), context))
    press(bot, context, "refs_done")
    press(bot, context, f"coupon:{cid}")
    assert "1980 ₽" in context.bot.send_message.call_args.args[1]
    press(bot, context, "build_pay:sbp")
    press(bot, context, "build_submit")
    order = bot.s.order(1)
    assert order["base_rub"] == 2200
    assert order["status"] == "requested"
    assert json.loads(order["options"])["characters"] == 3
    assert order["preferred_method"] == "sbp"
    assert bot.s.one("SELECT state FROM coupons WHERE id=?", (cid,))["state"] == "reserved"
    press(bot, context, "aui:accept:1", uid=99)
    run(bot.on_message(message(99, "Ready next week"), context))
    assert bot.s.order(1)["rub"] == 1980
    assert bot.s.order(1)["status"] == "quoted"
    press(bot, context, "paypick:1:sbp")
    assert bot.s.order(1)["external_amount"] == "1980"
    assert bot.s.order(1)["status"] == "quoted"
    press(bot, context, "aui:markpaid:1", uid=99)
    run(bot.on_message(message(99, "bank-op-42"), context))
    assert bot.s.order(1)["status"] == "paid"


def test_custom_character_surcharge_and_price_snapshot(bot):
    context = ctx()
    bot.s.set_setting("extra_character_percent", "50")
    press(bot, context, "product:classic_full")
    bot.s.set_setting("extra_character_percent", "200")
    bot.s.set_setting("background_rub", "1000")
    press(bot, context, "build_bg:1")
    assert "+300 ₽" in context.bot.send_message.call_args.kwargs["reply_markup"].inline_keyboard[0][0].text
    press(bot, context, "build_second:1")
    press(bot, context, "build_third:0")
    assert bot.amounts(bot.s.dialog(1)[1])["rub"] == 1300


def test_minecraft_skips_art_addons(bot):
    context = ctx()
    press(bot, context, "product:mc_skin")
    assert bot.s.dialog(1)[1]["step"] == "brief"
    with pytest.raises(RuleError):
        press(bot, context, "build_bg:1")


def test_disabled_and_removed_formats_cannot_be_ordered(bot):
    context = ctx()
    for slug in ("light_logo", "light_background", "mc_4d"):
        with pytest.raises(RuleError):
            press(bot, context, "product:" + slug)
    assert bot.s.one("SELECT title FROM art_products WHERE slug='light_chibi'")["title"] == "Чиби"


def test_no_slots_and_cancelled_draft(bot):
    context = ctx()
    for slot in bot.s.free_slots("classic_head"):
        bot.s.set_slot(slot["id"], False)
    press(bot, context, "product:classic_head")
    assert bot.s.dialog(1) is None
    assert "пока заняты" in context.bot.send_message.call_args.args[1]
    press(bot, context, "product:light_icon")
    assert bot.s.one("SELECT * FROM slot_holds WHERE user_id=1")
    press(bot, context, "studio")
    assert bot.s.one("SELECT * FROM slot_holds WHERE user_id=1") is None


def test_background_stars_must_be_configured_separately(bot):
    p = bot.s.one("SELECT * FROM art_products WHERE slug='classic_full'")
    p["stars"] = 700
    assert total(p, {"characters": 3, "background": True})["stars"] is None
    assert total(p, {"characters": 3, "background": True}, 400, 250)["stars"] == 2350
    assert total(p, {"characters": 3})["rub"] == 1800


def test_exchange_rates_are_explicit_and_rounded_up():
    assert method_amount(600, {"currency": "USD", "rate": "95"}) == "6.32"
    assert method_amount(600, {"currency": "RUB"}) == "600"
    for rate in (None, "0", "NaN", "abc"):
        with pytest.raises(RuleError):
            method_amount(600, {"currency": "USDT", "rate": rate})


def test_payment_switch_ownership_and_pending_guards(bot):
    bot.s.user(1)
    oid = bot.s.create_order(1, "classic_full", "brief", [])
    bot.s.quote(oid, 600, 700, "week")
    config = {"currency": "RUB", "instructions": "details"}
    with pytest.raises(RuleError):
        bot.s.choose_payment(2, oid, "sbp", config)
    old = bot.s.order(oid)["invoice"]
    bot.s.choose_payment(1, oid, "sbp", config)
    with pytest.raises(RuleError):
        bot.s.precheckout(1, old, 700, "XTR")
    assert bot.s.order(oid)["stars"] == 700
    bot.s.choose_payment(1, oid, "stars")
    fresh = bot.s.order(oid)["invoice"]
    assert fresh != old
    bot.s.precheckout(1, fresh, 700, "XTR")
    with pytest.raises(RuleError):
        bot.s.choose_payment(1, oid, "sbp", config)


def test_proof_and_auto_invoice_block_switch(bot):
    bot.s.user(1)
    oid = bot.s.create_order(1, "classic_full", "brief", [])
    bot.s.quote(oid, 600, 700, "week")
    config = {"currency": "RUB", "instructions": "details"}
    bot.s.choose_payment(1, oid, "sbp", config)
    bot.s.payment_proof(1, oid, {"text": "receipt"})
    with pytest.raises(RuleError):
        bot.s.choose_payment(1, oid, "stars")
    oid2 = bot.s.create_order(1, "classic_full", "brief", [])
    bot.s.quote(oid2, 600, 700, "week")
    bot.s.choose_payment(
        1, oid2, "ton", {"currency": "TON", "instructions": "API", "rate": "200", "provider": "cryptopay"}
    )
    bot.s.reserve_auto(oid2)
    with pytest.raises(RuleError):
        bot.s.choose_payment(1, oid2, "stars")


def test_admin_price_wizard_and_replayed_button(bot):
    context = ctx()
    press(bot, context, "aui:price:classic_full", uid=99)
    run(bot.on_message(message(99, "650"), context))
    button = context.bot.send_message.call_args.kwargs["reply_markup"].inline_keyboard[0][0].callback_data
    press(bot, context, button, uid=99)
    with pytest.raises(RuleError):
        press(bot, context, button, uid=99)
    button = context.bot.send_message.call_args.kwargs["reply_markup"].inline_keyboard[0][0].callback_data
    press(bot, context, button, uid=99)
    p = bot.s.one("SELECT * FROM art_products WHERE slug='classic_full'")
    assert p["rub"] == 650 and p["stars"] is None and p["points"] is None


def test_admin_promo_wizard_and_unauthorized_buttons(bot):
    context = ctx()
    with pytest.raises(RuleError):
        press(bot, context, "aui:promonew")
    press(bot, context, "aui:promonew", uid=99)
    for text in ("TIBRIS20", "20", "10", "30"):
        run(bot.on_message(message(99, text), context))
    assert bot.s.one("SELECT percent FROM promos WHERE code='tibris20'")["percent"] == 20


def test_admin_riddle_wizard_without_command_syntax(bot):
    context = ctx()
    press(bot, context, "aui:new:riddles", uid=99)
    for text in ("Что любит лиса?", "art", "10", "5"):
        run(bot.on_message(message(99, text), context))
    button = context.bot.send_message.call_args.kwargs["reply_markup"].inline_keyboard[0][0].callback_data
    press(bot, context, button, uid=99)
    assert bot.s.one("SELECT answer FROM riddles")["answer"] == "art"


def test_old_catalog_migrates_without_losing_history_or_prices(bot):
    s = bot.s
    s.user(1)
    with s.db(True) as c:
        c.execute(
            "INSERT INTO art_products(slug,category,title,rub,description) VALUES('mc_4d','minecraft','4D',400,'old')"
        )
        c.execute("INSERT INTO slots(slug,title) VALUES('mc_4d','old slot')")
    oid = s.create_order(1, "mc_4d", "old order", [])
    with s.db(True) as c:
        c.execute("UPDATE art_products SET rub=900 WHERE slug='classic_full'")
    reopened = Store(s.path)
    assert reopened.order(oid)["title"] == "4D"
    assert reopened.one("SELECT active FROM art_products WHERE slug='mc_4d'")["active"] == 0
    assert reopened.one("SELECT rub FROM art_products WHERE slug='classic_full'")["rub"] == 900


def test_message_chunks_respect_emoji_length():
    text = "🐾" * 6000
    chunks = list(text_chunks(text, 3900))
    assert "".join(chunks) == text
    assert all(len(c.encode("utf-16-le")) // 2 <= 3900 for c in chunks)


def test_upgrade_clears_only_legacy_unfinished_drafts(bot):
    s = bot.s
    s.user(1)
    s.user(2)
    oid = s.create_order(1, "classic_head", "accepted before upgrade", [])
    s.hold_slot(2, s.free_slots("classic_head")[0]["id"], "classic_head")
    s.save_dialog(2, "order", {"slug": "classic_head", "step": "brief"})
    with s.db(True) as c:
        c.execute("DELETE FROM settings WHERE key='simple_catalog_v3'")
    reopened = Store(s.path)
    assert reopened.dialog(2) is None
    assert reopened.one("SELECT * FROM slot_holds WHERE user_id=2") is None
    assert reopened.order(oid)["brief"] == "accepted before upgrade"


def skip_form(bot, context):
    data = bot.s.dialog(99)[1]
    press(bot, context, f"av:{data['nonce']}:{data['index']}:skip", uid=99)


def pick_form(bot, context, index):
    data = bot.s.dialog(99)[1]
    press(bot, context, f"av:{data['nonce']}:{data['index']}:{index}", uid=99)


def test_admin_shop_item_and_task_forms(bot):
    context = ctx()
    press(bot, context, "aui:new:shop_items", uid=99)
    for text in ("Sticker", "15", "A gift"):
        run(bot.on_message(message(99, text), context))
    skip_form(bot, context)
    skip_form(bot, context)
    pick_form(bot, context, 1)
    run(bot.on_message(message(99, "download link"), context))
    skip_form(bot, context)
    item = bot.s.one("SELECT * FROM shop_items")
    assert item["content"] == "download link" and item["manual_delivery"] == 0
    press(bot, context, "aui:tasktype:code", uid=99)
    for text in ("https://example.com", "Find code", "5"):
        run(bot.on_message(message(99, text), context))
    skip_form(bot, context)
    run(bot.on_message(message(99, "FOX"), context))
    task = bot.s.one("SELECT * FROM subscriptions")
    assert task["task_type"] == "code" and task["secret_code"].casefold() == "fox"


def test_admin_method_wizard_sets_exchange_rate(bot):
    context = ctx()
    press(bot, context, "aui:methodsetup:usdt:USDT", uid=99)
    run(bot.on_message(message(99, "USDT · TON network"), context))
    pick_form(bot, context, 0)
    run(bot.on_message(message(99, "95,5"), context))
    run(bot.on_message(message(99, "Address and TON network"), context))
    m = dict(bot.methods())["usdt"]
    assert m["provider"] == "manual" and m["rate"] == "95.5"
    assert method_amount(600, m) == "6.282723"


def test_unconfigured_automatic_methods_hidden(bot):
    bot.s.set_setting(
        "method:ton",
        json.dumps({"provider": "cryptopay", "currency": "TON", "rate": "200", "instructions": "API"}),
    )
    bot.s.set_setting("method:hipolink", json.dumps({"currency": "USD", "instructions": "url"}))
    assert bot.methods() == []


def test_free_coupon_and_base_points_orders(bot):
    context = ctx()
    bot.s.user(1)
    bot.s.add_promo("FREEART", 100)
    cid, _ = bot.s.claim_promo(1, "FREEART")
    press(bot, context, "product:mc_totem")
    run(bot.on_message(message(1, "Fox totem"), context))
    press(bot, context, "refs_done")
    press(bot, context, f"coupon:{cid}")
    press(bot, context, "build_pay:bonus")
    press(bot, context, "build_submit")
    press(bot, context, "aui:accept:1", uid=99)
    run(bot.on_message(message(99, "Next week"), context))
    press(bot, context, "redeem:1")
    assert bot.s.order(1)["status"] == "paid" and bot.s.order(1)["rub"] == 0
    with bot.s.db(True) as c:
        c.execute("UPDATE art_products SET points=12 WHERE slug='classic_head'")
    bot.s.adjust(1, 20)
    press(bot, context, "product:classic_head")
    press(bot, context, "build_bg:0")
    press(bot, context, "build_second:0")
    run(bot.on_message(message(1, "Fox portrait"), context))
    press(bot, context, "refs_done")
    press(bot, context, "build_pay:points")
    press(bot, context, "build_submit")
    press(bot, context, "aui:accept:2", uid=99)
    run(bot.on_message(message(99, "Next week"), context))
    press(bot, context, "redeem:2")
    assert bot.s.order(2)["status"] == "paid" and bot.s.balance(1) == 8


def test_admin_menu_routes_and_long_promo_callbacks(bot):
    context = ctx()
    bot.s.add_promo("A" * 64, 10)
    for screen in (
        "admin",
        "aui:catalog",
        "aui:product:classic_full",
        "aui:content",
        "aui:covers",
        "aui:texts",
        "aui:settings",
        "aui:promos",
        "aui:payments",
        "aui:methodnew",
        "aui:cube",
        "aui:list:riddles:0",
        "aui:users",
        "adminorders:0:all",
        "adminslots:0",
    ):
        press(bot, context, screen, uid=99)
        markup = context.bot.send_message.call_args.kwargs["reply_markup"]
        assert all(len(b.callback_data.encode()) <= 64 for row in markup.inline_keyboard for b in row)
