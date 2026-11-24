"""The terminal demo checks itself. This runs it once, so it cannot quietly break."""

from ordr import demo


async def test_every_scene_ends_as_designed(capsys):
    await demo.run()
    out = " ".join(capsys.readouterr().out.split())
    assert "All scenes ended as designed." in out
    assert "Not affiliated with Swiggy" in out
    assert "no AI model runs" in out
    assert out.count("[ok]") == 15


async def test_a_failed_check_is_not_hidden():
    try:
        demo.check(False, "x")
    except demo.DemoFailed:
        return
    raise AssertionError("check() let a failure pass")
