"""
tests/test_notifications.py — Mixtape

Regression tests for Issue #4: rating a song must notify the song's
original sharer, mirroring the playlist-add notification path. This
module had no coverage before, which is how a silently-missing
notification shipped.
"""

import pytest
from app import create_app, db
from models import User, Song, Notification
from services.notification_service import rate_song


@pytest.fixture
def app():
    app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:"})
    with app.app_context():
        db.create_all()
        yield app
        db.drop_all()


@pytest.fixture
def sharer_and_rater(app):
    """A sharer with one shared song, plus a second user who will rate it."""
    with app.app_context():
        sharer = User(username="aaliya", email="aaliya@example.com")
        rater = User(username="kenji", email="kenji@example.com")
        db.session.add_all([sharer, rater])
        db.session.flush()

        song = Song(title="Golden Hour", artist="Solange K", shared_by=sharer.id)
        db.session.add(song)
        db.session.commit()
        yield {"sharer": sharer, "rater": rater, "song": song}


def test_rating_creates_notification_for_sharer(app, sharer_and_rater):
    """Rating someone else's song creates a song_rated notification for them.

    This is the regression test for Issue #4: rate_song previously saved
    the Rating but never called create_notification.
    """
    with app.app_context():
        data = sharer_and_rater
        rate_song(data["rater"].id, data["song"].id, 5)

        notifications = Notification.query.filter_by(user_id=data["sharer"].id).all()
        assert len(notifications) == 1
        assert notifications[0].notification_type == "song_rated"
        assert data["rater"].username in notifications[0].body
        assert data["song"].title in notifications[0].body


def test_rating_own_song_does_not_self_notify(app, sharer_and_rater):
    """Rating your own song must not generate a notification to yourself."""
    with app.app_context():
        data = sharer_and_rater
        rate_song(data["sharer"].id, data["song"].id, 4)

        notifications = Notification.query.filter_by(user_id=data["sharer"].id).all()
        assert notifications == []


def test_rating_is_still_saved(app, sharer_and_rater):
    """The notification step must not interfere with saving the rating itself."""
    with app.app_context():
        data = sharer_and_rater
        rating = rate_song(data["rater"].id, data["song"].id, 3)
        assert rating.score == 3
        assert rating.song_id == data["song"].id
