from __future__ import annotations

from algo.providers.dhan.auth import TokenRotator


def test_token_rotator_callback_dedupe():
    rot = TokenRotator.instance()
    seen: list[str] = []

    def cb(tok: str) -> None:
        seen.append(tok)

    rot.on_renew(cb)
    rot.on_renew(cb)
    assert rot._on_renew.count(cb) == 1
    for hook in list(rot._on_renew):
        hook("abc")
    assert seen == ["abc"]
    rot.off_renew(cb)
    assert cb not in rot._on_renew
