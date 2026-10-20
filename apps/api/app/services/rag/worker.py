"""Single-worker durable index synchronization, with leased bounded retry."""
import argparse
import asyncio
import fcntl
import os
import psycopg
import httpx
from app.core.config import get_settings
from app.core.rag_config import load_rag_settings, ProviderRegistry, RagError, RagSettings
from app.db.session import initialize_meeting_store, open_meeting_db
from app.db.rag_session import open_rag_db, migrate_rag_db
from app.services.meetings.repository import MeetingRepository
from app.services.rag.chunking import chunk_transcript, snapshot_meeting
from app.services.rag.outbox import acknowledge
from app.services.rag.providers import ModelClient
from app.services.rag.repository import RagRepository


def _snapshot(sqlite: MeetingRepository, meeting_id: str):
    detail = sqlite.get_detail(meeting_id)
    return snapshot_meeting(detail) if detail and detail.meeting.status == 'completed' else None


async def synchronize_once(sqlite: MeetingRepository, rag: RagRepository, clients: ModelClient, settings: RagSettings) -> bool:
    rag.register_profiles(list(settings.profiles))
    rag.retire_profiles([p.id for p in settings.profiles])
    events = sqlite.connection.execute('SELECT meeting_id,max(id) FROM rag_outbox GROUP BY meeting_id').fetchall()
    through = {row[0]: row[1] for row in events}
    did_work = False
    # Purges are driven by authoritative state, not an old event's kind.
    for meeting_id in through:
        if _snapshot(sqlite, meeting_id) is None:
            rag.purge(meeting_id)
            acknowledge(sqlite.connection, meeting_id, through[meeting_id])
            did_work = True
    rows = sqlite.connection.execute("SELECT id FROM meetings WHERE status='completed' ORDER BY id").fetchall()
    for row in rows:
        snapshot = _snapshot(sqlite, row[0])
        if snapshot:
            for profile in settings.profiles:
                if rag.manifest(profile.id, snapshot.meeting_id) != snapshot.revision:
                    rag.queue(profile, snapshot)
    for profile_id, meeting_id, revision in rag.manifests():
        snapshot = _snapshot(sqlite, meeting_id)
        if snapshot is None:
            rag.purge(meeting_id)
    job = rag.claim()
    if job:
        did_work = True
        profile_id, meeting_id, revision = job
        profile = next(p for p in settings.profiles if p.id == profile_id)
        snapshot = _snapshot(sqlite, meeting_id)
        if snapshot is None:
            rag.purge(meeting_id)
        elif snapshot.revision == revision:
            try:
                chunks = chunk_transcript(snapshot)
                vectors = []
                batch_size = max(1, min(16, 65536 // (profile.dimensions * 32)))
                for offset in range(0, len(chunks), batch_size):
                    vectors.extend(await clients.embed(profile, [c.text for c in chunks[offset:offset+batch_size]]))
                current = _snapshot(sqlite, meeting_id)
                if current is None or current.revision != revision:
                    rag.purge(meeting_id)
                else:
                    rag.publish(snapshot, profile, chunks, vectors)
                    current = _snapshot(sqlite, meeting_id)
                    if current is None or current.revision != revision:
                        rag.purge(meeting_id)
            except RagError as error:
                rag.fail_job(profile_id, meeting_id, error.code, error.code in {'provider_timeout', 'provider_unavailable'})
    for row in rows:
        snapshot = _snapshot(sqlite, row[0])
        if snapshot and all(rag.manifest(p.id, snapshot.meeting_id) == snapshot.revision for p in settings.profiles):
            if snapshot.meeting_id in through:
                acknowledge(sqlite.connection, snapshot.meeting_id, through[snapshot.meeting_id])
    for profile in settings.profiles:
        rag.mark_ready(profile.id)
    return did_work


async def _run(args) -> None:
    settings = load_rag_settings(os.environ)
    if not settings.enabled:
        raise RagError('rag_disabled')
    data_dir = get_settings().meeting_data_dir
    initialize_meeting_store(data_dir)
    with (data_dir / 'rag-worker.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RagError('worker_already_running') from None
        has_migrated = False
        has_reset_jobs = False
        with open_meeting_db(data_dir) as sqlite:
            while True:
                try:
                    # A fresh connection also recovers server restarts and broken sockets.
                    with open_rag_db(settings) as connection:
                        if args.migrate and not has_migrated:
                            migrate_rag_db(connection)
                            has_migrated = True
                        rag = RagRepository(connection)
                        rag.register_profiles(list(settings.profiles))
                        if args.backfill and not has_reset_jobs:
                            connection.execute("UPDATE rag_index_jobs SET state='pending',attempts=0,next_attempt_at=now(),lease_until=NULL WHERE state='failed'")
                            has_reset_jobs = True
                        async with httpx.AsyncClient() as http:
                            worked = await synchronize_once(MeetingRepository(sqlite), rag, ModelClient(ProviderRegistry(settings), http), settings)
                except (psycopg.Error, RagError):
                    if args.once:
                        raise RagError('synchronization_unavailable') from None
                    worked = False
                if args.once:
                    break
                if not worked:
                    await asyncio.sleep(1)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ('once', 'migrate', 'backfill'):
        parser.add_argument('--' + flag, action='store_true')
    try:
        asyncio.run(_run(parser.parse_args()))
    except (RagError, psycopg.Error):
        raise SystemExit('RAG worker unavailable; check configuration and database') from None


if __name__ == '__main__':
    main()
