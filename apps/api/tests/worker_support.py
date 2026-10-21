"""Exercise child inference in-process so fake models never require downloads."""
import threading
from app.services.meetings.worker import run_claimed_meeting


def run_once_inline(repository, data_dir, settings, database):
    meeting = repository.claim_next()
    if meeting is None:
        return False
    started, owned = threading.Event(), threading.Event()
    started.set()
    owned.set()
    run_claimed_meeting(meeting.id, data_dir, settings, database, started, owned)
    return True
