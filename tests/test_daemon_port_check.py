"""v0.8.0.2: daemon refuses to start when port is in use."""
import socket


def test_check_port_available_returns_true_when_free():
    from systemu.scheduler.daemon import _check_port_available
    # Find a known-free ephemeral port
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    assert _check_port_available("127.0.0.1", port) is True


def test_check_port_available_returns_false_when_taken():
    from systemu.scheduler.daemon import _check_port_available
    # Bind + listen, hold it
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    port = s.getsockname()[1]
    try:
        assert _check_port_available("127.0.0.1", port) is False
    finally:
        s.close()


def test_find_listening_pid_returns_int_or_none_no_exception():
    """find_listening_pid must not raise; returns int or None."""
    from systemu.scheduler.daemon import _find_listening_pid
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    port = s.getsockname()[1]
    try:
        result = _find_listening_pid(port)
        # Either int (we found it via psutil) or None (psutil fail / permission)
        assert result is None or isinstance(result, int)
    finally:
        s.close()
