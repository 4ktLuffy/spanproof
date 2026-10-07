"""The extra replay scenarios must not move the published default, and must stay deterministic."""
import random

from spanproof import watch_replay as w

BASE = w.load(["results/live_agents1.jsonl", "results/corpus_dc_on.jsonl"])[:40]


def arrivals(**kw):
    t = w.schedule(BASE, random.Random(3), **kw)
    return [(s["span_id"], round(s["_arrive"], 6)) for sp in t for s in sp]


def test_default_schedule_ignores_new_options_when_unset():
    assert arrivals() == arrivals(max_duration=1800.0, mu=3.7, backlog_p=0.02, backlog_max=600.0, burst=None)


def test_burst_holds_spans_back_until_the_end_of_the_outage():
    start, length = 3 * 3600.0, 1200.0
    t = w.schedule(BASE, random.Random(3), burst=(start, length))
    arr = [s["_arrive"] for sp in t for s in sp]
    assert not any(start <= a < start + length for a in arr)  # nothing arrives during the outage
    assert any(start + length <= a < start + length + 30 for a in arr)  # it is flushed right after
    # negative control: without the burst some spans do arrive inside the window
    plain = [s["_arrive"] for sp in w.schedule(BASE, random.Random(3)) for s in sp]
    assert any(start <= a < start + length for a in plain)


def test_replay_is_deterministic_and_never_stores_twice_in_harsh_scenario():
    sc = dict(w.SCENARIOS["backlog-heavy"])
    a = w.run("watch", w.schedule(BASE, random.Random(1), **sc), 1, 0.1, 0.3)
    b = w.run("watch", w.schedule(BASE, random.Random(1), **sc), 1, 0.1, 0.3)
    assert a == b
    assert a["duplicates"] == 0
