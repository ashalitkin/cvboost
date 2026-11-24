from __future__ import annotations

from cvboost.notify.callbacks import handle_update
from tests.conftest import NOW
from tests.notify.helpers import OWNER, callback, err


def press(s, tg, sent, data, **kw):
    kw.setdefault("message_id", sent["message_id"])
    handle_update(s, tg.client(), OWNER, callback(data, **kw), NOW)


def decisions(s):
    return [dict(r) for r in s.conn.execute("SELECT * FROM decisions ORDER BY id")]


def buttons(tg):
    [edit] = tg.of("editMessageReplyMarkup")
    return [
        (b["text"], b["callback_data"])
        for row in edit["reply_markup"]["inline_keyboard"]
        for b in row
    ]


def silent(s, tg):
    assert decisions(s) == []
    assert tg.of("editMessageReplyMarkup") == [] and tg.of("answerCallbackQuery") == []


# --- only the owner ---


def test_foreign_user_is_ignored(s, tg, sent):
    press(s, tg, sent, f"i:{sent['id']}", user=999, chat=OWNER)
    silent(s, tg)


def test_owner_id_but_foreign_chat_is_ignored(s, tg, sent):
    press(s, tg, sent, f"i:{sent['id']}", user=OWNER, chat=-100123)
    silent(s, tg)


def test_foreign_chat_with_owner_text_is_ignored(s, tg, sent):
    press(s, tg, sent, f"cv:{sent['id']}", user=999, chat=999)
    silent(s, tg)
    assert s.conn.execute("SELECT COUNT(*) FROM cv_requests").fetchone()[0] == 0


def test_plain_message_and_other_updates_are_ignored(s, tg, sent):
    msg = {
        "update_id": 5,
        "message": {"message_id": 1, "from": {"id": 999}, "chat": {"id": 999}, "text": "hi"},
    }
    for u in (
        msg,
        {"update_id": 6},
        {"update_id": 7, "callback_query": "x"},
        {"update_id": 8, "callback_query": {"id": "a", "from": {"id": OWNER}, "data": "i:1"}},
    ):
        handle_update(s, tg.client(), OWNER, u, NOW)
    silent(s, tg)
    assert tg.calls == []


# --- owner's buttons ---


def test_interested_writes_decision_and_updates_buttons(s, tg, sent):
    press(s, tg, sent, f"i:{sent['id']}", update_id=42)
    [d] = decisions(s)
    assert (d["action"], d["channel"], d["tg_update_id"], d["alert_id"]) == (
        "interested",
        "telegram",
        42,
        sent["id"],
    )
    assert d["job_id"] == sent["job_id"] and d["canonical_id"] == sent["canonical_id"]
    assert [t for t, _ in buttons(tg)] == ["Interested ✓", "Prepare CV", "Undo"]
    [ans] = tg.of("answerCallbackQuery")
    assert ans["callback_query_id"] == "cbq1" and ans["text"]
    edit = tg.of("editMessageReplyMarkup")[0]
    assert edit["chat_id"] == OWNER and edit["message_id"] == sent["message_id"]


def test_no_asks_for_reason_without_a_decision_then_reason_is_stored(s, tg, sent):
    press(s, tg, sent, f"n:{sent['id']}")
    assert decisions(s) == []
    assert [t for t, _ in buttons(tg)] == ["Stack", "Rate", "Location", "Other", "Back"]
    tg.calls.clear()
    press(s, tg, sent, f"r:{sent['id']}:rate", update_id=2)
    [d] = decisions(s)
    assert (d["action"], d["reason"]) == ("rejected", "rate")
    assert [t for t, _ in buttons(tg)] == ["No: Rate", "Undo"]


def test_back_restores_initial(s, tg, sent):
    press(s, tg, sent, f"b:{sent['id']}")
    assert [t for t, _ in buttons(tg)] == ["Interested", "No", "Prepare CV"]
    assert decisions(s) == []


def test_cv_creates_request_once(s, tg, sent):
    press(s, tg, sent, f"cv:{sent['id']}", update_id=10)
    press(s, tg, sent, f"cv:{sent['id']}", update_id=11)
    ds = decisions(s)
    assert [d["action"] for d in ds] == ["cv_requested", "cv_requested"]  # both presses kept
    [r] = s.conn.execute("SELECT * FROM cv_requests").fetchall()
    assert r["status"] == "queued" and r["decision_id"] == ds[0]["id"]
    assert [t for t, _ in buttons_last(tg)] == ["CV: queued", "Undo"]


def buttons_last(tg):
    edit = tg.of("editMessageReplyMarkup")[-1]
    return [
        ((b["text"]), b["callback_data"])
        for row in edit["reply_markup"]["inline_keyboard"]
        for b in row
    ]


def test_undo_cancels_queued_request_but_not_one_in_progress(s, tg, sent):
    press(s, tg, sent, f"cv:{sent['id']}", update_id=1)
    press(s, tg, sent, f"u:{sent['id']}", update_id=2)
    assert [d["action"] for d in decisions(s)] == ["cv_requested", "undo"]
    assert s.conn.execute("SELECT status FROM cv_requests").fetchone()[0] == "cancelled"
    assert [t for t, _ in buttons_last(tg)] == ["Interested", "No", "Prepare CV"]
    press(s, tg, sent, f"cv:{sent['id']}", update_id=3)  # a fresh request is possible again
    s.conn.execute("UPDATE cv_requests SET status='in_progress' WHERE status='queued'")
    press(s, tg, sent, f"u:{sent['id']}", update_id=4)
    assert (
        s.conn.execute("SELECT status FROM cv_requests ORDER BY id DESC").fetchone()[0]
        == "in_progress"
    )


def test_same_update_twice_is_one_decision(s, tg, sent):
    press(s, tg, sent, f"i:{sent['id']}", update_id=7)
    press(s, tg, sent, f"i:{sent['id']}", update_id=7)
    assert len(decisions(s)) == 1
    assert len(tg.of("editMessageReplyMarkup")) == 2  # redelivery still repaints the buttons


def test_status_button_only_answers(s, tg, sent):
    press(s, tg, sent, f"x:{sent['id']}")
    assert decisions(s) == [] and tg.of("editMessageReplyMarkup") == []
    assert len(tg.of("answerCallbackQuery")) == 1


def test_unknown_data_and_unknown_alert_only_answer(s, tg, sent):
    for data in ("zzz", "i:abc", "r:1", f"i:{sent['id']}:stack", "i:99999", "r:1:seniority"):
        press(s, tg, sent, data)
    assert decisions(s) == [] and tg.of("editMessageReplyMarkup") == []
    assert len(tg.of("answerCallbackQuery")) == 6


def test_button_of_another_message_is_rejected(s, tg, sent):
    press(s, tg, sent, f"i:{sent['id']}", message_id=sent["message_id"] + 1)
    assert decisions(s) == [] and tg.of("answerCallbackQuery")[0]["text"] == "Job not found"


def test_not_modified_is_not_an_error_and_other_telegram_errors_keep_the_decision(s, tg, sent):
    tg.queue(
        "editMessageReplyMarkup",
        err(
            400,
            "Bad Request: message is not modified: specified new message content and reply markup "
            "are exactly the same",
        ),
    )
    press(s, tg, sent, f"i:{sent['id']}", update_id=1)
    assert len(decisions(s)) == 1 and len(tg.of("answerCallbackQuery")) == 1
    tg.queue("editMessageReplyMarkup", err(500, "Internal"))
    tg.queue("answerCallbackQuery", err(400, "query is too old"))
    press(s, tg, sent, f"n:{sent['id']}", update_id=2)  # no exception escapes
    press(s, tg, sent, f"r:{sent['id']}:stack", update_id=3)
    assert [d["reason"] for d in decisions(s)] == [None, "stack"]


def test_telegram_decisions_mark_the_group_seen(s, tg, sent):
    cid = s.canonical_of(sent["job_id"])
    assert s.seen_ids() == set()
    press(s, tg, sent, f"i:{sent['id']}", update_id=51)
    assert s.seen_ids() == {cid}
    s.conn.execute("DELETE FROM job_views")
    press(s, tg, sent, f"cv:{sent['id']}", update_id=52)
    assert s.seen_ids() == {cid}
    s.conn.execute("DELETE FROM job_views")
    press(s, tg, sent, f"u:{sent['id']}", update_id=53)
    assert s.seen_ids() == {cid}


# --- #70b: a button on an OLD (pre-70b) Russian alert still works; the answers are English ---

OLD_RU_ALERT = (
    "Senior Analytics Engineer — Acme\nСтавка: не указана\nЛокация: Europe (Remote)\n"
    "Fit 82 · data_engineer\nКратко: Контракт, удалённо."
)


def press_old(s, tg, sent, data, **kw):
    upd = callback(data, message_id=sent["message_id"], **kw)
    upd["callback_query"]["message"]["text"] = OLD_RU_ALERT  # what Telegram still shows
    upd["callback_query"]["message"]["reply_markup"] = {
        "inline_keyboard": [[{"text": "Интересно", "callback_data": data}]]
    }
    handle_update(s, tg.client(), OWNER, upd, NOW)


def test_old_russian_alert_every_button_acts_and_answers_in_english(s, tg, sent):
    a = sent["id"]
    steps = [
        (f"i:{a}", ["Interested ✓", "Prepare CV", "Undo"], "Marked as interested"),
        (f"u:{a}", ["Interested", "No", "Prepare CV"], "Undone"),
        (f"n:{a}", ["Stack", "Rate", "Location", "Other", "Back"], None),
        (f"r:{a}:stack", ["No: Stack", "Undo"], None),
        (f"u:{a}", ["Interested", "No", "Prepare CV"], "Undone"),
        (f"n:{a}", ["Stack", "Rate", "Location", "Other", "Back"], None),
        (f"b:{a}", ["Interested", "No", "Prepare CV"], None),
        (f"cv:{a}", ["CV: queued", "Undo"], "CV request created"),
        (f"x:{a}", None, None),
    ]
    for n, (data, labels, toast) in enumerate(steps, 1):
        tg.calls.clear()
        press_old(s, tg, sent, data, update_id=n)
        [ans] = tg.of("answerCallbackQuery")
        assert ans.get("text") == toast, data
        if labels is None:
            assert tg.of("editMessageReplyMarkup") == []  # 'x' is a status button
        else:
            [edit] = tg.of("editMessageReplyMarkup")
            got = [b["text"] for row in edit["reply_markup"]["inline_keyboard"] for b in row]
            assert got == labels, data
    assert [d["action"] for d in decisions(s)][:2] == ["interested", "undo"]


def test_unknown_and_foreign_buttons_answer_in_english(s, tg, sent):
    press(s, tg, sent, "garbage")
    assert tg.of("answerCallbackQuery")[0]["text"] == "Unknown button"
    tg.calls.clear()
    press(s, tg, sent, f"i:{sent['id']}:stack")  # a reason only goes with 'r'
    assert tg.of("answerCallbackQuery")[0]["text"] == "Unknown button"
    assert decisions(s) == []
