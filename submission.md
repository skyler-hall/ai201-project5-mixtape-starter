# Project 5: Mixtape Bug Hunt — Submission

Skyler Hall · AI201 Summer 2026 · branch: `bugfix/mixtape`

---

## AI Usage

I used Claude throughout this project, and it did substantially more than explain code — being transparent about that here.

**What the AI did:** After I gave it the repo link, Claude cloned the repo, read every service file, and identified candidate root causes for all five issues from reading alone. It then wrote reproduction scripts against a Flask test client to confirm each bug empirically, implemented the fixes, ran the test suite after each change, and drafted this submission doc.

**Where I verified or where the AI's first read was wrong:** The most important case was Issue #3. Claude's initial diagnosis (the `outerjoin` to `song_tags` multiplying rows) was correct at the SQL level, but its first reproduction attempt *failed* — the search endpoint returned exactly 1 result for a 3-tag song, contradicting both the user report and the diagnosis. Rather than accept the surface explanation, we ran the raw SQL directly (3 rows) vs. `Query.all()` (1 entity) and then built an isolated SQLAlchemy script outside the app to prove that the legacy `Query` API auto-de-duplicates full-entity rows in this SQLAlchemy version (2.0.51), masking the bug at the ORM layer even though the row multiplication is real. This is a case where the AI's diagnosis had to be checked against actual execution before I trusted it — and the check changed the story.

**Other verification steps:** every fix was validated by (a) running the existing pytest suite, (b) re-running the reproduction to confirm the reported behavior no longer occurs, and (c) checking the other side of the boundary for the boundary-condition bugs (Sunday→Monday still increments for #1; a listen from earlier *today* still appears for #2; an empty playlist still returns `[]` for #5). For the Issue #4 regression test, we checked out the pre-fix `notification_service.py` from git history and confirmed the new test fails against it before passing on the fixed code.

**Honest assessment of the division of labor:** the AI did the navigation, diagnosis, fixing, and verification; my role was directing the work, reviewing each fix and RCA, and understanding the reasoning well enough to defend it. The verification methodology (reproduce → fix → re-verify → boundary check) is documented per-bug below with the actual evidence produced.

---

## Codebase Map

*(Written after orientation, before any bug work.)*

**Structure and responsibilities:**

- `app.py` — Flask application factory (`create_app`) and the shared `db = SQLAlchemy()` object. Registers four blueprints with URL prefixes (`/songs`, `/playlists`, `/users`, `/feed`). The factory pattern is why `python app.py` double-imports and why `FLASK_APP=app:create_app flask run` is required.
- `models.py` — 6 models (`User`, `Tag`, `Song`, `ListeningEvent`, `Rating`, `Notification`, `Playlist`) plus 3 plain association tables (`friendships`, `song_tags`, `playlist_entries`). Notable details: `playlist_entries` is not just a join table — it carries `position`, `added_by`, and `added_at`, so playlist order is explicit. `Rating` has a unique constraint on `(user_id, song_id)`, so re-rating updates rather than duplicates. `Song.tags` uses `lazy="subquery"` eager loading, which turned out to matter for Issue #3. All timestamps are timezone-aware UTC at creation but SQLite stores them naive — `streak_service` explicitly re-attaches `tzinfo` when reading `last_listened_at` back.
- `routes/` — thin blueprints. Every route does input parsing/validation, delegates to exactly one service function, and formats the JSON response. No business logic lives here.
- `services/` — all business logic, one file per feature area: `streak_service`, `feed_service`, `search_service`, `notification_service`, `playlist_service`. One organizational quirk: `rate_song` and `add_to_playlist` live in `notification_service.py` rather than a song/playlist service — presumably because both are "actions that should notify," which is ironic given Issue #4 is that one of them doesn't.
- `seed_data.py` — creates 5 users with friendships, 13 songs with deliberately varying tag counts (0, 1, 3+ — the comment says the multi-tag ones "expose Issue #3"), 3 playlists, listening events spanning 2 weeks, and one example `song_added_to_playlist` notification so the working pattern is visible.
- `tests/` — coverage for streaks, search, and playlists. **No tests for feed or notifications** — the two untested services are where Issues #2 and #4 live.

**Data flow traced — a user rates a song:** `POST /songs/<song_id>/rate` → `routes/songs.py:rate()` parses `user_id` and `score` from JSON → `notification_service.rate_song()` validates the score range, looks up the song and rater, checks for an existing `Rating` row for that `(user, song)` pair (update vs. insert), commits. Pre-fix, the flow ended there; the parallel flow `POST /playlists/<id>/songs` → `add_to_playlist()` continues with a `create_notification()` call to the song's sharer, guarded by `song.shared_by != added_by_user_id`.

**Patterns noticed:** route→single-service-call delegation everywhere; UUID string PKs; `to_dict()` serializers on every model; self-action guard on notifications; explicit position ordering for playlists.

---

## Root Cause Analysis Entries

### Issue #1 — My listening streak keeps resetting

**How I reproduced it:** Called `update_listening_streak` directly with controlled datetimes rather than waiting for a real Sunday: set a user's `last_listened_at` to Saturday 2026-07-04 with `listening_streak = 12`, then called the function with `now` = Sunday 2026-07-05. Result: streak became **1**, not 13. Control run: Sunday→Monday with the same setup incremented normally to 13 — confirming the failure is specific to the update landing *on* a Sunday, matching kenji's report ("both times it was a Sunday"). The repo's own `test_streak_increments_on_sunday` also failed on the unfixed code.

**How I found the root cause:** README's issue table pointed to `streak_service.py`. The route chain is `POST /songs/<id>/listen` → `record_listening_event()` → `update_listening_streak()`. The consecutive-day branch reads `elif days_since_last == 1 and today.weekday() != 6:` — the extra weekday condition on a branch that should only care about day *deltas* was the immediate red flag. Confidence came from checking Python's convention: `date.weekday()` returns 6 for Sunday, so the condition is literally "increment on consecutive days, *except never on Sundays*."

**The root cause:** The increment branch required both `days_since_last == 1` **and** `today.weekday() != 6`. Sunday is `weekday() == 6`, so a consecutive-day listen occurring on any Sunday failed the second condition, fell through to the `else` branch, and reset the streak to 1. This looks like leftover "week boundary" logic — but streaks are defined by consecutive calendar days, and days don't stop being consecutive across a weekend. The docstring's own rules ("If the user listened yesterday: streak increments") make no mention of weekdays.

**Fix and side-effect check:** Removed `and today.weekday() != 6`, leaving `elif days_since_last == 1:`. Side-effect checks: full `test_streaks.py` suite passes (5/5), covering new-user init, same-day no-double-count, skipped-day reset, normal consecutive increment, and the Sunday case. Both sides of the boundary verified: Sat→Sun now increments, Sun→Mon still increments, Sat→Mon (skipped Sunday) still resets.

*Commit: `fix: remove Sunday exclusion that reset streaks on consecutive-day listens`*

---

### Issue #2 — Friends Listening Now shows people from yesterday

**How I reproduced it:** As nova (friend of darius), deleted darius's listening events and inserted a single event timestamped yesterday at 23:00 UTC, then called `get_friends_listening_now(nova.id)` "the next morning." Darius appeared in the feed with yesterday's song — exactly nova's report of stale entries "hanging around until the same time the next day."

**How I found the root cause:** README pointed to `feed_service.py`. The route is `GET /feed/<id>/listening-now` → `get_friends_listening_now()`. The cutoff line was immediately suspicious: `cutoff = datetime.now(timezone.utc) - RECENT_THRESHOLD` with `RECENT_THRESHOLD = timedelta(hours=24)`. The confidence moment was matching the math to the symptom: a rolling 24-hour window means an 11pm listen remains "recent" until 11pm the next day — which is precisely the reported behavior ("stuff from yesterday evening keeps hanging around until the same time the next day"). The symptom *is* the window's signature.

**The root cause:** "Listening now / listened today" was implemented as a rolling 24-hour lookback instead of a calendar-day boundary. `now - 24h` at 9am Monday reaches back to 9am Sunday, so anything from Sunday evening qualifies. The feature's contract (per the user report and the feed's name) is "friends who have listened *today*," which requires the cutoff to be the start of the current day, not now-minus-a-duration.

**Fix and side-effect check:** Changed the cutoff to the start of the current UTC day: `datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)`, and removed the now-unused `RECENT_THRESHOLD` constant and `timedelta` import. Boundary checks on both sides: the yesterday-11pm event no longer appears; a listen from 5 minutes ago (same day) does appear. Side-effect check: `get_activity_feed()` in the same file is *documented* as unfiltered by recency and shares no code with the cutoff — verified it still returns older events. One consciously accepted limitation: "today" is UTC-today since the app stores no user timezones; that's consistent with how every other timestamp in the app is handled.

*Commit: `fix: filter Friends Listening Now to today instead of a rolling 24h window`*

---

### Issue #3 — The same song keeps showing up twice in search

This one has a twist, and the reproduction section is honest about it.

**How I reproduced it:** First attempt *failed*: `GET /songs/search?q=Anthem` returned exactly **1** result for "Crown Heights Anthem" (3 tags), contradicting both simone's report and my initial read of the code. Instead of moving on, I dropped below the ORM: running the service's generated SQL directly against the seeded database returned **3 rows** — `['Crown Heights Anthem', 'Crown Heights Anthem', 'Crown Heights Anthem']`, one per tag. So the query genuinely multiplies rows; something between the SQL and the JSON was collapsing them. An isolated SQLAlchemy script (fresh in-memory DB, one 3-tag entity, same outerjoin) confirmed the mechanism: legacy `session.query(...).all()` returned 1 entity while 2.0-style `select(...).scalars().all()` on the identical statement returned 3 — the legacy Query API auto-de-duplicates full-entity result rows by primary key, and that de-duplication is masking the bug in this environment (SQLAlchemy 2.0.51).

**How I found the root cause:** `search_service.py:search_songs()`. The query outer-joins `song_tags` — but the `WHERE` clause filters only on `Song.title` and `Song.artist`, and tags are serialized through the `Song.tags` relationship inside `to_dict()`. The join contributes nothing to filtering or loading; its only effect is turning one song row into one row per `(song, tag)` pair. The seed data was even structured to expose this (its comment marks the 3-tag songs as "the ones that expose Issue #3"). The confidence moment was the raw-SQL row count exactly matching the tag count.

**The root cause:** A purposeless `OUTER JOIN` onto `song_tags` in a query whose filter and output need only the `Song` table. Standard SQL join fan-out: N tag rows → N result rows per matching song (songs with 0 tags still return one row because the join is *outer*). Whether users see the duplicates depends on the result-consumption API: legacy `Query.all()` de-duplicates entities and hides it; raw SQL, `select()`-style execution, or counting rows (e.g., pagination, `COUNT(*)`) exposes it. The user report is consistent with the code path that existed when it was filed; the fix targets the actual defect rather than the version-dependent symptom.

**Fix and side-effect check:** Removed the `.outerjoin(...)` (and the now-unused `Tag`/`song_tags` imports) rather than papering over the fan-out with `.distinct()` — the join shouldn't exist at all, and removing it also fixes latent breakage for row counts or any future migration off the legacy Query API. Side-effect checks: all 5 search tests pass; a 3-tag song returns once *with its tags list intact* (tags load via the relationship, proving the join was never needed for them); 0-tag and 1-tag songs still return exactly once; empty-match returns `[]`.

*Commit: `fix: drop tag-table join in song search that duplicated rows per tag`*

---

### Issue #4 — Notified on playlist add but not on rating

**How I reproduced it:** Via the HTTP API against seeded data: kenji `POST /songs/<song_id>/rate` (score 5) on a song shared by simone. The rating saved (201, visible on the song), but simone's notification count stayed at **0** — the notification is not delayed or misaddressed; it is never created. This matches aaliya's report exactly: rating saved, notification list empty.

**How I found the root cause:** The README's example call chain points `POST /songs/<id>/rate` at `notification_service.rate_song()`. The project hint said the cause was architectural, so I compared the two flows line-by-line as suggested: `add_to_playlist()` ends with a guarded `create_notification(...)` call after its commit; `rate_song()` ends with `db.session.commit(); return rating`. The confidence moment was structural: the function performs validation, upsert, commit, return — there is no notification code to be buggy. The `create_notification` helper itself works (the seed data contains a real playlist-add notification created through it), so the defect is a missing call site, not a broken helper.

**The root cause:** `rate_song()` never calls `create_notification()`. The notification layer has a clear pattern — after committing a friend's action on a shared song, notify `song.shared_by` unless the actor *is* the sharer — and the rating path simply doesn't implement it. Nothing fails, nothing logs; the step is absent, which is why the symptom is a silent 100% miss rather than flaky delivery.

**Fix and side-effect check:** After the rating commit, added the same guarded pattern as the working path: if `song.shared_by != user_id`, `create_notification(user_id=song.shared_by, notification_type="song_rated", body=f"{rater.username} rated your song '{song.title}' {score} stars.")`. Side-effect checks: rating a friend's song now yields exactly one `song_rated` notification retrievable via `GET /users/<id>/notifications`; rating your **own** song creates no self-notification (mirroring `add_to_playlist`'s guard); the rating upsert behavior is unchanged (verified re-rating updates the score); full test suite passes. Also wrote regression tests — see stretch section.

*Commit: `fix: create song_rated notification for the sharer when a song is rated`*

---

### Issue #5 — The last song in a playlist never shows up

**How I reproduced it:** `GET /playlists/<id>/songs` for seeded "Friday Energy": the `playlist_entries` table holds **7** rows for the playlist; the endpoint returned `count: 6`, missing the highest-position song. The repo's `test_playlist_returns_all_songs` and `test_playlist_returns_songs_in_order` also fail on the unfixed code (5 seeded songs, 4 returned). darius's "adding a song frees the previous one and hides the new one" follows directly: the hidden song is always whatever currently sorts last.

**How I found the root cause:** `playlist_service.get_playlist_songs()`. The query itself is correct — join through `playlist_entries`, filter by playlist, `ORDER BY position ASC`. The bug is on the return line: `return [song.to_dict() for song in songs[:-1]]`. The confidence moment was that `[:-1]` — "everything except the last element" — is a one-token exact explanation of "exactly one song is always missing, and it's always the most recently added": songs are ordered ascending by position, so the last element is always the newest addition.

**The root cause:** A `[:-1]` slice on the ordered result list unconditionally discards the final element before serialization. Because ordering is ascending by `position` and new songs get the next position, the discarded element is deterministically the most recently added song. This is likely a leftover from debugging or an off-by-one misunderstanding (perhaps confusing it with dropping a sentinel/header row); the function's own docstring says "This function returns all songs in the playlist," contradicting its body.

**Fix and side-effect check:** Changed `songs[:-1]` to `songs`. Side-effect checks: endpoint now returns all 7 entries for Friday Energy including the last-positioned song; ordering unchanged (`test_playlist_returns_songs_in_order` passes); the empty-playlist case still returns `[]` (that behavior came from the query returning no rows — `[:-1]` on an empty list was coincidentally harmless, so removing it changes nothing there); `test_empty_playlist_returns_empty_list` passes; full suite green.

*Commit: `fix: return all playlist songs instead of slicing off the last one`*

---

## Stretch: Regression Test

`tests/test_notifications.py` — regression coverage for Issue #4, the one bug that lived in a completely untested module (notice `tests/` covered streaks, search, and playlists — the two services without tests, feed and notifications, are exactly where two of the five bugs lived).

Three tests: (1) rating someone else's song creates exactly one `song_rated` notification for the sharer containing the rater's username and song title — this is the test that would have caught the bug, verified by checking out the pre-fix `notification_service.py` from git history and confirming it fails there and passes on the fixed code; (2) rating your own song creates no self-notification; (3) the rating itself is still persisted correctly, guarding against the notification step breaking the original behavior.

*Commit: `test: add regression tests for song_rated notifications (Issue #4)`*

---

## Commit Log

```
6c37577 test: add regression tests for song_rated notifications (Issue #4)
38daa63 fix: return all playlist songs instead of slicing off the last one
eb86e45 fix: create song_rated notification for the sharer when a song is rated
1727905 fix: drop tag-table join in song search that duplicated rows per tag
1680b81 fix: filter Friends Listening Now to today instead of a rolling 24h window
0bca333 fix: remove Sunday exclusion that reset streaks on consecutive-day listens
```

(Screenshot of `git log --oneline` attached separately per submission requirements.)

Final state: 16/16 tests passing, all five reported behaviors no longer reproduce.
