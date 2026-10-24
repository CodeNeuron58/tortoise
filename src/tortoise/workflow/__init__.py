"""The Temporal layer: one durable workflow per council per week.

Messages (``messages.py``) are flat, JSON-serialisable dataclasses crossing the
activity boundary — no dates, no decimals, nothing the payload converter could
choke on. The workflow (``weekly_watch.py``) contains no I/O of its own: it
chains activities, filters in plain code, and catches the specific failures the
plan's six failure tests exercise. Activities (``activities.py``) do all real
work and own retry policies so retries are visible in the workflow history.
"""
