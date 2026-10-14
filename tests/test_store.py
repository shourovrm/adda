import pytest

from adda.models import Message
from adda.store import NotFoundError, Store


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "adda.db")


def make_message(thread_id: str, text: str, author: str = "owner", **extra) -> Message:
    role = "owner" if author == "owner" else "seat"
    return Message(thread_id=thread_id, author=author, role=role, text=text, **extra)


def test_create_and_get_thread(store):
    thread = store.create_thread("Design review", mode="manual")
    fetched = store.get_thread(thread.id)
    assert fetched.title == "Design review"
    assert fetched.mode == "manual"
    assert fetched.ended_at is None
    assert fetched.created_at.tzinfo is not None


def test_list_threads_newest_first(store):
    first = store.create_thread("first")
    second = store.create_thread("second")
    assert [thread.id for thread in store.list_threads()] == [second.id, first.id]


def test_set_mode_and_invalid_mode(store):
    thread = store.create_thread("t")
    assert store.set_mode(thread.id, "roundrobin").mode == "roundrobin"
    assert store.get_thread(thread.id).mode == "roundrobin"
    with pytest.raises(ValueError, match="mode"):
        store.set_mode(thread.id, "chaos")
    with pytest.raises(ValueError, match="mode"):
        store.create_thread("bad", mode="chaos")


def test_end_thread_sets_timezone_aware_ended_at(store):
    thread = store.create_thread("t")
    ended = store.end_thread(thread.id)
    assert ended.ended_at is not None
    assert ended.ended_at.tzinfo is not None
    assert store.get_thread(thread.id).ended_at == ended.ended_at


def test_unknown_thread_raises_not_found(store):
    with pytest.raises(NotFoundError):
        store.get_thread("missing")
    with pytest.raises(NotFoundError):
        store.set_mode("missing", "manual")
    with pytest.raises(NotFoundError):
        store.end_thread("missing")


def test_messages_list_in_order_and_last_n(store):
    thread = store.create_thread("t")
    for number in range(5):
        store.add_message(make_message(thread.id, f"message {number}"))
    assert [m.text for m in store.list_messages(thread.id)] == [f"message {n}" for n in range(5)]
    assert [m.text for m in store.list_messages(thread.id, last=2)] == ["message 3", "message 4"]
    assert store.list_messages(thread.id, last=0) == []


def test_messages_are_scoped_to_their_thread(store):
    first = store.create_thread("a")
    second = store.create_thread("b")
    store.add_message(make_message(first.id, "in first"))
    assert store.list_messages(second.id) == []


def test_mentions_cost_and_meta_round_trip(store):
    thread = store.create_thread("t")
    sent = make_message(
        thread.id,
        "hello @beta",
        author="alpha",
        mentions=["beta"],
        cost_usd=0.0123,
        meta={"bid": 7.5, "model": "haiku", "latency_ms": 420},
    )
    store.add_message(sent)
    (loaded,) = store.list_messages(thread.id)
    assert loaded == sent


def test_seat_sessions_get_set_overwrite(store):
    thread = store.create_thread("t")
    assert store.get_session(thread.id, "alpha") is None
    store.set_session(thread.id, "alpha", "session-1")
    assert store.get_session(thread.id, "alpha") == "session-1"
    store.set_session(thread.id, "alpha", "session-2")
    assert store.get_session(thread.id, "alpha") == "session-2"
    assert store.get_session(thread.id, "beta") is None


def test_search_finds_by_word_with_title_and_snippet(store):
    thread = store.create_thread("Cache discussion")
    store.add_message(make_message(thread.id, "We should use a write-through cache here"))
    store.add_message(make_message(thread.id, "Unrelated remark"))
    hits = store.search("cache")
    assert len(hits) == 1
    assert hits[0].thread_title == "Cache discussion"
    assert "cache" in hits[0].snippet.lower()
    assert hits[0].message.text.startswith("We should")


@pytest.mark.parametrize("query", ['"quoted', "key:value", "star*", "NEAR(a b)", "a OR", "'"])
def test_search_special_characters_do_not_raise(store, query):
    thread = store.create_thread("t")
    store.add_message(make_message(thread.id, "plain text"))
    assert isinstance(store.search(query), list)


def test_search_empty_query_returns_empty_list(store):
    assert store.search("") == []
    assert store.search("   ") == []
