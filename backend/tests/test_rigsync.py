"""The write-retry guarantees that used to live in player._RigSync now live in
output.RestTransport: a channel counts as on the wire only after its PUT
succeeds (so a failed write is re-sent next tick), and the full state is
re-asserted every KEYFRAME_S so anything silently dropped self-heals."""
from haunt_director.output import NCH, RestTransport, TransportError


class Http:
    def __init__(self):
        self.puts, self.fail_next = [], False
    def put(self, path, body):
        if self.fail_next:
            self.fail_next = False
            raise TransportError("boom")
        self.puts.append((path, body))
        return {}
    def close(self):
        pass


def _frame(**kw):
    f = bytearray(NCH)
    for k, v in kw.items():
        f[int(k[1:]) - 1] = v
    return bytes(f)


def test_failed_write_is_resent_next_tick():
    h = Http(); tr = RestTransport(h, "haunt"); tr.open()
    tr.send(_frame(), 0.0)
    h.fail_next = True
    try:
        tr.send(_frame(c2=200), 0.04)
    except TransportError:
        pass
    h.puts.clear()
    tr.send(_frame(c2=200), 0.08)
    assert ("/overlays/range/2", {"Value": 200}) in h.puts


def test_unchanged_channels_are_not_resent_between_keyframes():
    h = Http(); tr = RestTransport(h, "haunt"); tr.open()
    tr.send(_frame(c1=15), 0.0)
    h.puts.clear()
    tr.send(_frame(c1=15), 0.04)
    assert [p for p in h.puts if p[0].startswith("/overlays/range/")] == []


def test_keyframe_reasserts_everything():
    h = Http(); tr = RestTransport(h, "haunt"); tr.open()
    tr.send(_frame(c1=15), 0.0)
    h.puts.clear()
    tr.send(_frame(c1=15), RestTransport.KEYFRAME_S + 0.01)
    ranges = [p for p in h.puts if p[0].startswith("/overlays/range/")]
    sent = {int(c) for p, _ in ranges for c in p.rsplit("/", 1)[1].split(",")}
    assert sent == set(range(1, NCH + 1))
