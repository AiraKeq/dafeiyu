"""Scoped pre-waking command gate, without changing the pinned AstrBot image.
Native command argument errors can send before plugin observers. This owned
runtime wrapper stops managed command events before those filters are invoked.
"""


def install_guard(predicate):
    from astrbot.core.pipeline.waking_check.stage import WakingCheckStage
    current = WakingCheckStage.process
    original = getattr(current, '_whale_command_original', current)

    async def guarded(stage, event):
        if predicate(event):
            event.clear_result()
            event.stop_event()
            return
        return await original(stage, event)

    guarded._whale_command_original = original
    WakingCheckStage.process = guarded
    return WakingCheckStage, guarded, original


def remove_guard(receipt):
    if not receipt:
        return
    stage, wrapper, original = receipt
    # A newer reload may already own the class. Never remove that newer gate.
    if stage.process is wrapper:
        stage.process = original
