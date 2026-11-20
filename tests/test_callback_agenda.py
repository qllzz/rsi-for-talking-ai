from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from zhiyu import storage
from zhiyu.storage import Store, CallResult


@pytest.fixture
def sample(tmp_path, monkeypatch):
    store=Store(tmp_path/'calls.db'); now=[2000000000]; monkeypatch.setattr(storage.time,'time',lambda:now[0])
    def create(kind='callback', case='POP-001'):
        sid=uuid4().hex;store.begin(sid,case,'text','baseline')
        store.save_result(CallResult(session_id=sid,case_id=case,channel='phone',disposition=kind,identity_verified=False,acknowledged=False,turn_count=1,revision='baseline'),ticket_kind=kind)
        return store.result_artifacts(sid)['ticket'],sid
    return store,now,create


def test_schedule_persists_blocks_then_allows_at_boundary(sample):
    store,now,create=sample;ticket,sid=create();when=now[0]+90
    assert store.schedule_callback(ticket,when,0)['version']==1
    assert Store(store.path).callback_agenda()[0]['scheduled_at']==when
    with pytest.raises(ValueError,match='未到'):store.begin(uuid4().hex,'POP-001','text','baseline',ticket)
    now[0]=when;child=uuid4().hex;store.begin(child,'POP-001','text','baseline',ticket)
    assert store.callback_agenda()[0]['status']=='in_progress'
    with pytest.raises(ValueError):store.schedule_callback(ticket,None,1)
    now[0]+=storage.FOLLOWUP_LEASE_SECONDS;assert store.callback_agenda()[0]['status']=='pending'
    store.schedule_callback(ticket,None,1)
    assert len([e for e in store.detail(sid)['events'] if e['type']=='callback_schedule'])==2


def test_concurrent_edit_is_not_silently_overwritten(sample):
    store,now,create=sample;ticket,_=create()
    def attempt(delay):
        try:store.schedule_callback(ticket,now[0]+delay,0);return True
        except ValueError:return False
    with ThreadPoolExecutor(2) as p:assert sorted(p.map(attempt,[300,600]))==[False,True]


@pytest.mark.parametrize('delay',[-1,365*86400+1])
def test_invalid_times_do_not_mutate(sample,delay):
    store,now,create=sample;ticket,_=create()
    with pytest.raises(OverflowError):store.schedule_callback(ticket,now[0]+delay,0)
    assert store.callback_agenda()[0]['version']==0


def test_only_unconsumed_callback_tickets(sample):
    store,now,create=sample;handoff,_=create('handoff');ticket,_=create()
    with pytest.raises(KeyError):store.schedule_callback(handoff,None,0)
    child=uuid4().hex;store.begin(child,'POP-001','text','baseline',ticket)
    store.save_result(CallResult(session_id=child,case_id='POP-001',channel='phone',disposition='notified',identity_verified=True,acknowledged=True,turn_count=4,revision='baseline'))
    assert store.callback_agenda()==[]
    with pytest.raises(ValueError):store.schedule_callback(ticket,now[0]+10,0)


def test_queue_not_truncated_and_ordered(sample):
    store,now,create=sample
    for _ in range(60):create()
    early,_=create(case='POP-002');late,_=create(case='POP-003');store.schedule_callback(late,now[0]+200,0);store.schedule_callback(early,now[0]+100,0)
    queue=store.callback_agenda();assert len(queue)==62 and [t['id'] for t in queue[:2]]==[early,late]
