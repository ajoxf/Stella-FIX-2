from types import SimpleNamespace

from fixtrader.commands import apply_command
from fixtrader.config import TraderConfig, VenueConfig
from fixtrader.gateway import SessionState
from fixtrader.atomicfile import write_json
from fixtrader.webapp import create_app


def test_tt_landing_page_is_connection_dashboard_and_desk_is_available(tmp_path):
    path = tmp_path / 'config.json'
    cfg = TraderConfig(path=str(path))
    cfg.venues['TT-UAT'] = VenueConfig(name='TT-UAT', host='example.invalid', fix_version='FIX.4.2')
    cfg.save()
    client = create_app(str(path)).test_client()
    for route in ('/', '/connection'):
        page = client.get(route)
        assert page.status_code == 200
        assert b'FIX connection dashboard' in page.data
        assert b'id="connect"' in page.data
        assert b'id="disconnect"' in page.data
        assert b'id="reconnect"' in page.data
    assert b'contract-template' in client.get('/desk').data
    instruments = client.get('/instruments')
    assert instruments.status_code == 200
    for element in ('explore-dialog', 'explore-exchange', 'explore-type', 'explore-product', 'explore-contract', 'review-dialog'):
        assert ('id="' + element + '"').encode() in instruments.data
    # The market ladders and the account section left this page: the desk
    # has the ladder, the Account tab has the positions and P&L.
    for gone in ('ladders', 'account-summary', 'floating-pnl', 'realized-pnl'):
        assert ('id="' + gone + '"').encode() not in instruments.data
    assert b'href="/account"' in instruments.data


def test_fix_pages_share_one_navigation_and_visual_skin(tmp_path):
    path = tmp_path / 'config.json'
    cfg = TraderConfig(path=str(path))
    cfg.save()
    client = create_app(str(path)).test_client()
    routes = ('/', '/connection', '/instruments', '/account', '/logs', '/settings', '/exchanges')
    nav_targets = ('/desk', '/instruments', '/account', '/connection', '/exchanges', '/logs', '/settings')

    for route in routes:
        response = client.get(route)
        assert response.status_code == 200
        assert b'class="app-nav"' in response.data, route
        assert b'app_nav.css' in response.data, route
        for target in nav_targets:
            assert f'href="{target}"'.encode() in response.data, route
        # Trading first, then the connection pages.
        nav = response.data[response.data.index(b'<nav'):response.data.index(b'</nav>')]
        where = [nav.index(f'href="{t}"'.encode()) for t in nav_targets]
        assert where == sorted(where), route
    for route in ('/connection', '/instruments', '/logs'):
        assert b'fix_pages.css' in client.get(route).data


def test_controls_execute_only_on_the_engine_owned_venue():
    calls = []
    gateway = SimpleNamespace(venue=SimpleNamespace(name='TT-UAT'),
        start=lambda: calls.append('start'), stop=lambda: calls.append('stop'),
        reconnect=lambda: calls.append('reconnect'),
        state=lambda: SessionState.DOWN, diagnose=lambda: [])
    engine = SimpleNamespace(gateway=gateway, simulated=False)
    for action in ('fix_connect', 'fix_disconnect', 'fix_reconnect'):
        assert apply_command(engine, {'action': action, 'args': {'venue': 'TT-UAT'}})['ok']
    assert calls == ['start', 'stop', 'reconnect']
    result = apply_command(engine, {'action': 'fix_disconnect', 'args': {'venue': 'other'}})
    assert result['ok'] is False
    assert calls == ['start', 'stop', 'reconnect']


def test_sequence_gap_blocks_connect_and_reconnect_but_allows_disconnect():
    calls = []
    gateway = SimpleNamespace(
        venue=SimpleNamespace(name='TT-UAT'),
        _sessions={'Order Routing': SimpleNamespace(state=SimpleNamespace(
            error='FIX sequence mismatch on 5: expected 1, received 4'))},
        start=lambda: calls.append('start'), stop=lambda: calls.append('stop'),
        reconnect=lambda: calls.append('reconnect'),
        state=lambda: SessionState.ERROR, diagnose=lambda: [])
    engine = SimpleNamespace(gateway=gateway, simulated=False)

    for action in ('fix_connect', 'fix_reconnect'):
        result = apply_command(engine, {'action': action, 'args': {'venue': 'TT-UAT'}})
        assert result['ok'] is False
        assert 'sequence mismatch' in result['error'].lower()
    result = apply_command(engine, {'action': 'fix_disconnect', 'args': {'venue': 'TT-UAT'}})

    assert result['ok'] is True
    assert calls == ['stop']
    result = apply_command(engine, {'action': 'fix_disconnect', 'args': {'venue': 'other'}})
    assert result['ok'] is False
    assert calls == ['stop']


def test_web_command_api_refuses_reconnect_when_snapshot_has_sequence_gap(tmp_path):
    config_path = tmp_path / 'config.json'
    status_path = tmp_path / 'status.json'
    commands_path = tmp_path / 'commands.jsonl'
    config = TraderConfig(path=str(config_path))
    config.venues['TT-UAT'] = VenueConfig(name='TT-UAT', host='or.example')
    config.save()
    write_json(str(status_path), {'engine': {'alive': True, 'fix_connection': {
        'sessions': [{'name': 'Order Routing', 'error':
            'FIX sequence mismatch on 5: expected 1, received 4'}]}}})
    client = create_app(str(config_path), str(status_path), str(commands_path)).test_client()

    response = client.post('/api/command', json={
        'action': 'fix_reconnect', 'args': {'venue': 'TT-UAT'}})

    assert response.status_code == 409
    assert response.json['ok'] is False
    assert 'sequence mismatch' in response.json['error'].lower()
    assert not commands_path.exists()
