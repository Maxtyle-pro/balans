"""Coordinate physical deletion with a database-and-media backup."""
from contextlib import contextmanager
import fcntl,os

@contextmanager
def storage_lock(root,exclusive=False):
    fd=os.open(root/'.backup.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'rb') as f:
        fcntl.flock(f,fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        yield
