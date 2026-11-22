"""An event arriving between the queue check and Event.clear() must not be lost."""
import threading


def test_event_enqueued_during_clear_is_not_lost():
    from mcp_serve_plugin.bridge import EventBridge, QueueEvent

    bridge = EventBridge()

    class RacingEvent(threading.Event):
        raced = False

        def clear(self):
            if not RacingEvent.raced:
                RacingEvent.raced = True
                bridge._enqueue(QueueEvent(cursor=0, type="message", session_key="s",
                                           data={"content": "URGENT"}))
            super().clear()

    bridge._new_event = RacingEvent()
    event = bridge.wait_for_event(after_cursor=0, timeout_ms=1)
    assert event is not None and event["content"] == "URGENT"
