"""Black-box test suite for the Rust giveaway bot.

The tests are Python; the bot is Rust. ``harness.py`` starts the compiled
``gwbot-harness`` binary and drives the real service over a JSON-line protocol,
so these tests exercise the shipped rules, SQL and schema rather than a Python
re-implementation of them.

Nothing here needs a Discord token, a network connection or a Turso account:
every test gets a fresh SQLite file and a fake clock.
"""
