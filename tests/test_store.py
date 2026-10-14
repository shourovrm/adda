import sqlite3

import pytest

from adda.models import Message
from adda.store import NotFoundError, Store

# The v0.1 schema exactly as shipped, to test the in-place upgrade.
VERSION_ONE_SCHEMA = """
CREATE TABLE threads (
    id TEXT PRIMARY KEY, title TEXT NOT NULL, created_at TEXT NOT NULL,
    ended_at TEXT, mode TEXT NOT NULL
);
CREATE TABLE messages (
    rowid INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE,
    thread_id TEXT NOT NULL REFERENCES threads(id), author TEXT NOT NULL, role TEXT NOT NULL,
    text TEXT NOT NULL, created_at TEXT NOT NULL, mentions TEXT NOT NULL, cost_usd REAL,
    meta TEXT NOT NULL
);
CREATE INDEX messages_by_thread ON messages(thread_id, rowid);
CREATE VIRTUAL TABLE messages_fts USING fts5(
    text, author, content='messages', content_rowid='rowid'
);
CREATE TRIGGER messages_after_insert AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, text, author) VALUES (new.rowid, new.text, new.author);
END;
CREATE TABLE seat_sessions (
    thread_id TEXT NOT NULL REFERENCES threads(id), seat TEXT NOT NULL,
    session_id TEXT NOT NULL, PRIMARY KEY (thread_id, seat)
);
INSERT INTO threads VALUES ('t1', 'Old discussion', '2026-01-01T00:00:00+00:00', NULL, 'bidding');
INSERT INTO messages (id, thread_id, author, role, text, created_at, mentions, cost_usd, meta)
VALUES ('m1', 't1', 'claude', 'seat', 'zebra crossing design', '2026-01-01T00:00:01+00:00',
        '[]', 0.01, '{}');
INSERT INTO seat_sessions VALUES ('t1', 'claude', 'session-abc');
"""


@pytest.fixture
def store():
    store = Store(":memory:")
    yield store
    store.close()


def seat_message(store, room_id, seat, text="hello", **fields):
    """A seat message in the room's newest thread (a fresh room has exactly one)."""
    return Message(
        room_id=room_id,
        thread_id=store.newest_thread(room_id).id,
        seat_id=seat.id,
        author=seat.name,
        role="seat",
        text=text,
        **fields,
    )


def test_create_and_get_room_round_trip(store):
    room = store.create_room("Title", folder="/tmp/x", mode="manual", liveliness=3, budget_usd=2.5)
    loaded = store.get_room(room.id)
    assert loaded.title == "Title"
    assert (loaded.folder, loaded.mode, loaded.liveliness) == ("/tmp/x", "manual", 3)
    assert (loaded.budget_usd, loaded.spent_usd, loaded.seats) == (2.5, 0.0, [])
    assert loaded.ended_at is None


def test_list_rooms_newest_first_with_seats(store):
    older = store.create_room("older")
    newer = store.create_room("newer")
    store.add_seat(newer.id, name="Ravi", program="claude")
    rooms = store.list_rooms()
    assert [room.title for room in rooms] == ["newer", "older"]
    assert [seat.name for seat in rooms[0].seats] == ["Ravi"]
    assert older.id == rooms[1].id


def test_invalid_room_values_are_rejected(store):
    with pytest.raises(ValueError):
        store.create_room("x", mode="chaos")
    room = store.create_room("x")
    with pytest.raises(ValueError):
        store.update_room(room.id, liveliness=11)
    with pytest.raises(ValueError):
        store.update_room(room.id, spent_usd=5)
    with pytest.raises(NotFoundError):
        store.get_room("missing")


def test_update_room_end_room_and_add_spend(store):
    room = store.create_room("x")
    updated = store.update_room(room.id, title="y", liveliness=0, budget_usd=4, mode="roundrobin")
    assert (updated.title, updated.liveliness, updated.budget_usd) == ("y", 0, 4)
    store.add_spend(room.id, 0.25)
    assert store.add_spend(room.id, 0.5).spent_usd == pytest.approx(0.75)
    assert store.end_room(room.id).ended_at is not None


def test_seats_get_positions_handles_and_order(store):
    room = store.create_room("x")
    first = store.add_seat(room.id, name="Scout 2", program="pi", model="m", thinking="high")
    second = store.add_seat(room.id, name="Ravi", program="claude", persona="architecture")
    assert (first.position, second.position) == (0, 1)
    assert first.handle == "scout2"
    assert first.available is False
    assert [seat.name for seat in store.list_seats(room.id)] == ["Scout 2", "Ravi"]
    store.update_seat(second.id, position=-1)
    assert [seat.name for seat in store.list_seats(room.id)] == ["Ravi", "Scout 2"]


def test_duplicate_handles_and_bad_programs_are_rejected(store):
    room = store.create_room("x")
    store.add_seat(room.id, name="Scout 2", program="pi")
    with pytest.raises(ValueError):
        store.add_seat(room.id, name="scout2", program="pi")
    with pytest.raises(ValueError):
        store.add_seat(room.id, name="Other", program="vim")
    with pytest.raises(ValueError):
        store.add_seat(room.id, name="  ", program="pi")
    # The same name in another room is fine.
    other_room = store.create_room("y")
    store.add_seat(other_room.id, name="Scout 2", program="pi")


def test_update_seat_fields_and_clearing_with_none(store):
    room = store.create_room("x")
    seat = store.add_seat(room.id, name="A", program="claude", model="opus", instruction="be brief")
    updated = store.update_seat(
        seat.id, model=None, thinking="low", enabled=False, color="#111111", instruction=None
    )
    assert updated.model is None
    assert updated.instruction is None
    assert (updated.thinking, updated.enabled, updated.color) == ("low", False, "#111111")
    with pytest.raises(ValueError):
        store.update_seat(seat.id, room_id="other")
    with pytest.raises(NotFoundError):
        store.update_seat("missing", color="#000000")


def test_rename_seat_rewrites_its_authors_in_its_room_only(store):
    room = store.create_room("x")
    other_room = store.create_room("y")
    seat = store.add_seat(room.id, name="Scout1", program="pi")
    other_seat = store.add_seat(other_room.id, name="Scout1", program="pi")
    store.add_message(seat_message(store, room.id, seat))
    store.add_message(seat_message(store, other_room.id, other_seat))
    store.add_message(
        Message(
            room_id=room.id,
            thread_id=store.newest_thread(room.id).id,
            author="owner",
            role="owner",
            text="hi",
        )
    )

    renamed = store.rename_seat(seat.id, "Zed")

    assert renamed.name == "Zed"
    assert [message.author for message in store.list_messages(room.id)] == ["Zed", "owner"]
    assert store.list_messages(other_room.id)[0].author == "Scout1"
    assert store.list_seats(room.id)[0].handle == "zed"


def test_rename_to_an_existing_handle_changes_nothing(store):
    room = store.create_room("x")
    taken = store.add_seat(room.id, name="Taken", program="pi")
    seat = store.add_seat(room.id, name="Mine", program="pi")
    store.add_message(seat_message(store, room.id, seat))
    with pytest.raises(ValueError):
        store.rename_seat(seat.id, "taken")
    assert store.get_seat(seat.id).name == "Mine"
    assert store.list_messages(room.id)[0].author == "Mine"
    assert taken.name == "Taken"


def test_sessions_survive_rename_and_are_deleted_on_remove(store):
    room = store.create_room("x")
    seat = store.add_seat(room.id, name="A", program="claude")
    store.set_session(store.newest_thread(room.id).id, seat.id, "s1")
    store.set_session(store.newest_thread(room.id).id, seat.id, "s2")
    store.rename_seat(seat.id, "B")
    assert store.get_session(store.newest_thread(room.id).id, seat.id) == "s2"

    store.add_message(seat_message(store, room.id, seat))
    store.remove_seat(seat.id)
    assert store.get_session(store.newest_thread(room.id).id, seat.id) is None
    assert store.list_seats(room.id) == []
    assert len(store.list_messages(room.id)) == 1
    with pytest.raises(NotFoundError):
        store.remove_seat(seat.id)


def test_changing_program_drops_the_session_but_changing_model_keeps_it(store):
    room = store.create_room("x")
    seat = store.add_seat(room.id, name="A", program="claude")
    store.set_session(store.newest_thread(room.id).id, seat.id, "s1")
    store.update_seat(seat.id, model="opus")
    assert store.get_session(store.newest_thread(room.id).id, seat.id) == "s1"
    store.update_seat(seat.id, program="pi")
    assert store.get_session(store.newest_thread(room.id).id, seat.id) is None


def test_last_room_seats_uses_newest_room_with_seats(store):
    assert store.last_room_seats() == []
    older = store.create_room("older")
    store.add_seat(older.id, name="Old", program="claude", persona="architecture")
    store.create_room("newest but empty")
    seats = store.last_room_seats()
    assert [seat.name for seat in seats] == ["Old"]
    assert seats[0].persona == "architecture"
    newest = store.create_room("newest")
    store.add_seat(newest.id, name="B", program="pi", position=1)
    store.add_seat(newest.id, name="A", program="pi", position=0)
    assert [seat.name for seat in store.last_room_seats()] == ["A", "B"]


def test_messages_round_trip_and_last_n(store):
    room = store.create_room("x")
    seat = store.add_seat(room.id, name="A", program="claude")
    for number in range(5):
        store.add_message(
            seat_message(
                store,
                room.id,
                seat,
                text=f"m{number}",
                mentions=[seat.id],
                cost_usd=0.1,
                meta={"woken_by": "bid"},
            )
        )
    messages = store.list_messages(room.id)
    assert [message.text for message in messages] == [f"m{n}" for n in range(5)]
    assert messages[0].seat_id == seat.id
    assert messages[0].mentions == [seat.id]
    assert messages[0].meta == {"woken_by": "bid"}
    assert [message.text for message in store.list_messages(room.id, last=2)] == ["m3", "m4"]


def test_search_returns_room_title_and_markerless_snippet(store):
    room = store.create_room("Zebra room")
    seat = store.add_seat(room.id, name="A", program="claude")
    store.add_message(
        seat_message(store, room.id, seat, text="the zebra crossing needs a redesign")
    )
    hits = store.search("zebra")
    assert len(hits) == 1
    assert hits[0].room_title == "Zebra room"
    assert "zebra" in hits[0].snippet
    assert "<" not in hits[0].snippet and "[" not in hits[0].snippet


def test_search_treats_syntax_characters_literally(store):
    room = store.create_room("x")
    seat = store.add_seat(room.id, name="A", program="claude")
    store.add_message(seat_message(store, room.id, seat, text="plain words"))
    assert store.search('"unbalanced NEAR( col:on') == []
    assert store.search("   ") == []


def test_upgrade_from_version_one_database(tmp_path):
    database_path = tmp_path / "adda.db"
    old_connection = sqlite3.connect(database_path)
    old_connection.executescript(VERSION_ONE_SCHEMA)
    old_connection.commit()
    old_connection.close()

    store = Store(database_path)
    room = store.get_room("t1")
    assert (room.title, room.folder, room.liveliness) == ("Old discussion", "", 6)
    assert (room.budget_usd, room.spent_usd, room.seats) == (0.0, 0.0, [])

    messages = store.list_messages("t1")
    assert messages[0].text == "zebra crossing design"
    assert messages[0].seat_id is None
    assert messages[0].author == "claude"
    # The existing FTS index still finds old messages and the join uses rooms.
    assert store.search("zebra")[0].room_title == "Old discussion"

    # The upgraded database is fully usable: new messages are indexed, seats work.
    seat = store.add_seat("t1", name="Ravi", program="claude")
    store.add_message(seat_message(store, "t1", seat, text="quokka"))
    assert store.search("quokka")[0].message.seat_id == seat.id
    store.set_session(store.newest_thread("t1").id, seat.id, "s")
    assert store.get_session(store.newest_thread("t1").id, seat.id) == "s"
    store.close()

    # Opening it again is a no-op, not a second upgrade.
    reopened = Store(database_path)
    assert reopened.get_room("t1").title == "Old discussion"
    reopened.close()


def test_database_without_the_eagerness_column_is_upgraded_in_place(tmp_path):
    import sqlite3

    database_path = tmp_path / "v02.db"
    # A v0.2 database: the room_seats table has no eagerness column.
    connection = sqlite3.connect(database_path)
    connection.executescript(
        """
        CREATE TABLE rooms (id TEXT PRIMARY KEY, title TEXT NOT NULL, created_at TEXT NOT NULL,
            ended_at TEXT, mode TEXT NOT NULL, folder TEXT NOT NULL DEFAULT '',
            liveliness INTEGER NOT NULL DEFAULT 6, budget_usd REAL NOT NULL DEFAULT 0,
            spent_usd REAL NOT NULL DEFAULT 0);
        CREATE TABLE room_seats (id TEXT PRIMARY KEY, room_id TEXT NOT NULL, name TEXT NOT NULL,
            program TEXT NOT NULL, model TEXT, thinking TEXT NOT NULL DEFAULT 'default',
            persona TEXT, instruction TEXT, color TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1, position INTEGER NOT NULL DEFAULT 0);
        INSERT INTO rooms (id, title, created_at, mode)
            VALUES ('r1', 'old', '2026-01-01', 'bidding');
        INSERT INTO room_seats (id, room_id, name, program, color)
            VALUES ('s1', 'r1', 'Ravi', 'claude', '#fff');
        """
    )
    connection.commit()
    connection.close()

    store = Store(database_path)
    assert store.get_room("r1").seats[0].eagerness == 5
    store.update_seat("s1", eagerness=9)
    assert store.get_seat("s1").eagerness == 9
    store.close()
    Store(database_path).close()  # a second open must not try to add the column again


def test_add_seat_stores_eagerness_and_rejects_values_outside_zero_to_ten():
    store = Store(":memory:")
    room = store.create_room("t")
    seat = store.add_seat(room.id, name="A", program="claude", eagerness=0)
    assert store.get_seat(seat.id).eagerness == 0
    with pytest.raises(ValueError, match="eagerness"):
        store.add_seat(room.id, name="B", program="claude", eagerness=11)
    with pytest.raises(ValueError, match="eagerness"):
        store.update_seat(seat.id, eagerness=-1)


def test_database_without_max_concurrent_is_upgraded_in_place(tmp_path):
    database_path = tmp_path / "v03.db"
    connection = sqlite3.connect(database_path)
    connection.executescript(
        """
        CREATE TABLE rooms (id TEXT PRIMARY KEY, title TEXT NOT NULL, created_at TEXT NOT NULL,
            ended_at TEXT, mode TEXT NOT NULL, folder TEXT NOT NULL DEFAULT '',
            liveliness INTEGER NOT NULL DEFAULT 6, budget_usd REAL NOT NULL DEFAULT 0,
            spent_usd REAL NOT NULL DEFAULT 0);
        INSERT INTO rooms (id, title, created_at, mode)
            VALUES ('r1', 'old', '2026-01-01', 'bidding');
        """
    )
    connection.commit()
    connection.close()

    store = Store(database_path)
    assert store.get_room("r1").max_concurrent == 2
    store.update_room("r1", max_concurrent=4)
    assert store.get_room("r1").max_concurrent == 4
    store.close()
    Store(database_path).close()  # a second open must not try to add the column again


def test_max_concurrent_is_stored_and_limited_to_one_through_four(store):
    room = store.create_room("voices", max_concurrent=3)
    assert store.get_room(room.id).max_concurrent == 3
    assert store.create_room("default").max_concurrent == 2
    for bad_value in (0, 5, 2.5, True, "2"):
        with pytest.raises(ValueError, match="max_concurrent"):
            store.update_room(room.id, max_concurrent=bad_value)
        with pytest.raises(ValueError, match="max_concurrent"):
            store.create_room("bad", max_concurrent=bad_value)


# The v0.4 schema exactly as shipped, to test the upgrade to threads (SPEC v0.5 F2).
VERSION_FOUR_SCHEMA = """
CREATE TABLE rooms (
    id TEXT PRIMARY KEY, title TEXT NOT NULL, created_at TEXT NOT NULL, ended_at TEXT,
    mode TEXT NOT NULL, folder TEXT NOT NULL DEFAULT '', liveliness INTEGER NOT NULL DEFAULT 6,
    budget_usd REAL NOT NULL DEFAULT 0, spent_usd REAL NOT NULL DEFAULT 0,
    max_concurrent INTEGER NOT NULL DEFAULT 2
);
CREATE TABLE room_seats (
    id TEXT PRIMARY KEY, room_id TEXT NOT NULL REFERENCES rooms(id), name TEXT NOT NULL,
    program TEXT NOT NULL, model TEXT, thinking TEXT NOT NULL DEFAULT 'default', persona TEXT,
    instruction TEXT, color TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
    position INTEGER NOT NULL DEFAULT 0, eagerness INTEGER NOT NULL DEFAULT 5
);
CREATE TABLE messages (
    rowid INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE,
    room_id TEXT NOT NULL REFERENCES rooms(id), seat_id TEXT, author TEXT NOT NULL,
    role TEXT NOT NULL, text TEXT NOT NULL, created_at TEXT NOT NULL, mentions TEXT NOT NULL,
    cost_usd REAL, meta TEXT NOT NULL
);
CREATE INDEX messages_by_room ON messages(room_id, rowid);
CREATE VIRTUAL TABLE messages_fts USING fts5(
    text, author, content='messages', content_rowid='rowid'
);
CREATE TRIGGER messages_after_insert AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, text, author) VALUES (new.rowid, new.text, new.author);
END;
CREATE TABLE seat_sessions (
    room_id TEXT NOT NULL REFERENCES rooms(id), seat_id TEXT NOT NULL,
    session_id TEXT NOT NULL, PRIMARY KEY (room_id, seat_id)
);
INSERT INTO rooms (id, title, created_at, ended_at, mode)
VALUES ('r1', 'Queue design', '2026-09-01T00:00:00+00:00', NULL, 'bidding'),
       ('r2', 'Old talk', '2026-08-01T00:00:00+00:00', '2026-08-02T00:00:00+00:00', 'bidding');
INSERT INTO room_seats (id, room_id, name, program, color)
VALUES ('s1', 'r1', 'Ravi', 'claude', '#fff'), ('s2', 'r2', 'Tuli', 'pi', '#000');
INSERT INTO messages (id, room_id, seat_id, author, role, text, created_at, mentions, meta)
VALUES ('m1', 'r1', NULL, 'owner', 'owner', 'which queue?', '2026-09-01T00:00:01+00:00',
        '[]', '{}'),
       ('m2', 'r1', 's1', 'Ravi', 'seat', 'sqlite wombat queue', '2026-09-01T00:00:02+00:00',
        '[]', '{}'),
       ('m3', 'r2', 's2', 'Tuli', 'seat', 'old line', '2026-08-01T00:00:01+00:00', '[]', '{}');
INSERT INTO seat_sessions VALUES ('r1', 's1', 'session-r1'), ('r2', 's2', 'session-r2');
"""


def test_upgrade_from_version_four_gives_every_room_one_thread(tmp_path):
    database_path = tmp_path / "v4.db"
    connection = sqlite3.connect(database_path)
    connection.executescript(VERSION_FOUR_SCHEMA)
    connection.close()

    store = Store(database_path)

    first_threads = store.list_threads("r1")
    assert [(thread.title, thread.message_count) for thread in first_threads] == [
        ("Queue design", 2)
    ]
    thread_id = first_threads[0].id
    assert [message.id for message in store.list_thread_messages(thread_id)] == ["m1", "m2"]
    assert all(message.thread_id == thread_id for message in store.list_messages("r1"))
    assert (first_threads[0].last_author, first_threads[0].last_line) == (
        "Ravi",
        "sqlite wombat queue",
    )
    # Sessions follow their room into its thread, so seats keep resuming.
    assert store.get_session(thread_id, "s1") == "session-r1"

    # A room that had ended in v0.4 finished its discussion: the thread ends, the room reopens.
    old_thread = store.list_threads("r2")[0]
    assert old_thread.ended_at is not None
    assert store.get_room("r2").ended_at is None
    assert store.get_session(old_thread.id, "s2") == "session-r2"

    hit = store.search("wombat")[0]
    assert (hit.room_title, hit.thread_title, hit.message.thread_id) == (
        "Queue design",
        "Queue design",
        thread_id,
    )
    store.close()

    # Opening the upgraded file again changes nothing.
    reopened = Store(database_path)
    assert [thread.id for thread in reopened.list_threads("r1")] == [thread_id]
    reopened.close()


def test_new_room_has_a_first_thread_titled_like_the_room(store):
    room = store.create_room("Billing")
    assert [thread.title for thread in room.threads] == ["Billing"]
    assert room.threads[0].room_id == room.id
    assert store.newest_thread(room.id).id == room.threads[0].id


def test_thread_crud_and_summaries(store):
    room = store.create_room("Project")
    first_id = room.threads[0].id
    second = store.create_thread(room.id, "  Second topic  ")
    assert second.title == "Second topic"
    seat = store.add_seat(room.id, name="Ravi", program="claude")
    store.add_message(
        Message(
            room_id=room.id,
            thread_id=second.id,
            seat_id=seat.id,
            author="Ravi",
            role="seat",
            text="\n\nfirst line\nsecond line",
        )
    )
    store.add_message(
        Message(room_id=room.id, thread_id=second.id, author="system", role="system", text="note")
    )

    summaries = store.list_threads(room.id)
    assert [summary.id for summary in summaries] == [second.id, first_id]  # newest first
    assert (summaries[0].message_count, summaries[0].last_author, summaries[0].last_line) == (
        1,
        "Ravi",
        "first line",
    )
    assert (summaries[1].message_count, summaries[1].last_line) == (0, None)
    assert [summary.id for summary in store.get_room(room.id).threads] == [second.id, first_id]

    assert store.rename_thread(second.id, "Renamed").title == "Renamed"
    assert store.thread_summary(second.id).title == "Renamed"
    assert [message.text for message in store.list_thread_messages(first_id)] == []
    assert len(store.list_thread_messages(second.id, last=1)) == 1

    ended = store.end_thread(second.id)
    assert ended.ended_at is not None
    # The newest open thread wins over a newer ended one.
    assert store.newest_thread(room.id).id == first_id
    store.end_thread(first_id)
    assert store.newest_thread(room.id).id == second.id
    assert store.get_room(room.id).ended_at is None


def test_thread_errors(store):
    room = store.create_room("Project")
    with pytest.raises(ValueError):
        store.create_thread(room.id, "   ")
    with pytest.raises(NotFoundError):
        store.create_thread("nope", "x")
    with pytest.raises(NotFoundError):
        store.get_thread("nope")
    with pytest.raises(ValueError):
        store.rename_thread(room.threads[0].id, "")
    other_room = store.create_room("Other")
    with pytest.raises(ValueError, match="belongs to room"):
        store.add_message(
            Message(
                room_id=room.id,
                thread_id=other_room.threads[0].id,
                author="owner",
                role="owner",
                text="hi",
            )
        )


def test_sessions_are_per_thread(store):
    room = store.create_room("Project")
    seat = store.add_seat(room.id, name="Ravi", program="claude")
    first_id = room.threads[0].id
    second = store.create_thread(room.id, "Second")
    store.set_session(first_id, seat.id, "session-one")
    assert store.get_session(second.id, seat.id) is None
    store.set_session(second.id, seat.id, "session-two")
    assert store.get_session(first_id, seat.id) == "session-one"


# Access levels (SPEC v0.6 G1) -----------------------------------------------------------------


def test_database_from_version_five_gets_access_columns_in_place(tmp_path):
    database_path = tmp_path / "v05.db"
    first_store = Store(database_path)
    room = first_store.create_room("old")
    seat = first_store.add_seat(room.id, name="Ada", program="claude")
    first_store.close()
    # Turn it back into a v0.5 database: no access column on either table.
    connection = sqlite3.connect(database_path)
    connection.execute("ALTER TABLE rooms DROP COLUMN access")
    connection.execute("ALTER TABLE room_seats DROP COLUMN access")
    connection.commit()
    connection.close()

    store = Store(database_path)
    upgraded_room = store.get_room(room.id)
    assert upgraded_room.access == "talk"
    assert upgraded_room.seats[0].access is None
    assert upgraded_room.seats[0].effective_access == "talk"
    store.update_room(room.id, access="work")
    assert store.get_seat(seat.id).effective_access == "work"
    store.close()
    Store(database_path).close()  # a second open must not add the columns again


def test_effective_access_is_the_seat_level_else_the_room_level(store):
    room = store.create_room("project", access="look")
    inheriting = store.add_seat(room.id, name="A", program="claude")
    explicit = store.add_seat(room.id, name="B", program="pi", access="work")
    assert (inheriting.access, inheriting.effective_access) == (None, "look")
    assert (explicit.access, explicit.effective_access) == ("work", "work")

    store.update_room(room.id, access="talk")
    seats = {seat.name: seat for seat in store.get_room(room.id).seats}
    assert seats["A"].effective_access == "talk"
    assert seats["B"].effective_access == "work"

    store.update_seat(explicit.id, access=None)  # null clears the seat's own level
    assert store.get_seat(explicit.id).effective_access == "talk"


def test_access_values_are_checked_for_rooms_and_seats(store):
    with pytest.raises(ValueError, match="access"):
        store.create_room("bad", access="root")
    room = store.create_room("ok")
    with pytest.raises(ValueError, match="access"):
        store.update_room(room.id, access=None)
    with pytest.raises(ValueError, match="access"):
        store.add_seat(room.id, name="A", program="claude", access="root")
    seat = store.add_seat(room.id, name="B", program="claude")
    with pytest.raises(ValueError, match="access"):
        store.update_seat(seat.id, access="root")
