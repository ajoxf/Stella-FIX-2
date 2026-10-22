"""Config: blank vs zero, atomic saves, and secrets that never come back."""
import json
import os

import pytest

from fixtrader import config as cfgmod
from fixtrader.config import (ContractConfig, TraderConfig, VenueConfig,
                              env_key_for, write_env_value)


def test_a_blank_field_takes_the_desk_default(iron_ore, desk):
    s = iron_ore.settings_with_defaults(desk)
    assert s['entry_threshold'] == desk['DEFAULT_ENTRY_THRESHOLD']


def test_zero_is_a_real_number_and_is_not_treated_as_blank():
    """A loop that skips falsy values can only ever SET an override, never
    clear one — and silently ignores a commission of 0, which is a genuine
    statement about a contract."""
    c = ContractConfig(key='k', commission_per_contract=0.0,
                       entry_cooldown_seconds=0)
    s = c.settings_with_defaults(dict(cfgmod.DEFAULT_SETTINGS,
                                      DEFAULT_COMMISSION_PER_CONTRACT=1.2,
                                      DEFAULT_ENTRY_COOLDOWN_SECONDS=60))
    assert s['commission_per_contract'] == 0.0
    assert s['entry_cooldown_seconds'] == 0.0


def test_an_empty_string_is_blank_and_does_take_the_default():
    c = ContractConfig(key='k', commission_per_contract='')
    s = c.settings_with_defaults(dict(cfgmod.DEFAULT_SETTINGS,
                                      DEFAULT_COMMISSION_PER_CONTRACT=1.2))
    assert s['commission_per_contract'] == 1.2


def test_an_override_wins_over_the_desk(iron_ore, desk):
    iron_ore.overrides['entry_threshold'] = 2.5
    assert iron_ore.settings_with_defaults(desk)['entry_threshold'] == 2.5


def test_a_venue_must_declare_its_environment():
    """There is no default. Assuming UAT would eventually assume it about a
    live venue."""
    with pytest.raises(ValueError) as e:
        VenueConfig(name='Orient', environment='')
    assert 'UAT' in str(e.value) and 'PROD' in str(e.value)
    with pytest.raises(ValueError):
        VenueConfig(name='Orient', environment='DEMO')


def test_uat_and_prod_are_separate_venues():
    cfg = TraderConfig()
    cfg.venues['uat'] = VenueConfig('Orient UAT', environment='UAT')
    assert cfg.environment_label == 'UAT'
    cfg.venues['prod'] = VenueConfig('Orient PROD', environment='PROD')
    # One live venue makes the whole screen live, deliberately.
    assert cfg.has_production_venue and cfg.environment_label == 'PROD'


def test_the_venue_carries_the_env_key_never_the_secret(monkeypatch):
    v = VenueConfig('Orient SGX UAT', environment='UAT')
    assert v.password_env == 'FIX_ORIENT_SGX_UAT'
    monkeypatch.setenv('FIX_ORIENT_SGX_UAT', 'hunter2')
    assert v.password == 'hunter2'
    # ...and neither dict form contains it, masked or otherwise
    assert 'hunter2' not in json.dumps(v.to_dict())
    assert 'hunter2' not in json.dumps(v.to_public_dict())
    assert v.to_public_dict()['password_set'] is True


def test_a_password_in_a_config_file_is_ignored_not_loaded():
    v = VenueConfig.from_dict('x', {'environment': 'UAT',
                                    'password': 'should-not-be-here'})
    assert not hasattr(v, 'password_value')
    assert 'should-not-be-here' not in json.dumps(v.to_dict())


def test_env_key_is_sanitised():
    assert env_key_for('Orient SGX / UAT #1') == 'FIX_ORIENT_SGX_UAT_1'


def test_save_and_reload_round_trips(tmp_path, iron_ore):
    path = str(tmp_path / 'config.json')
    cfg = TraderConfig(path=path)
    cfg.venues['sgx'] = VenueConfig('sgx', environment='UAT', host='h', port=1)
    cfg.contracts[iron_ore.key] = iron_ore
    cfg.settings['PRICE_REFRESH_SEC'] = 0.5
    cfg.save()

    back = TraderConfig.from_file(path)
    assert back.contracts[iron_ore.key].tick_value == 1.0
    assert back.venues['sgx'].environment == 'UAT'
    assert back.settings['PRICE_REFRESH_SEC'] == 0.5


def test_a_save_never_leaves_a_truncated_file(tmp_path):
    """`open(path,'w')` truncates first, and a reader in that window has read
    an EMPTY config and written it back, deleting every account."""
    path = str(tmp_path / 'config.json')
    cfg = TraderConfig(path=path)
    cfg.venues['sgx'] = VenueConfig('sgx', environment='UAT')
    cfg.save()
    for _ in range(20):
        cfg.save()
        with open(path) as fh:
            data = json.load(fh)          # never raises: always complete
        assert 'sgx' in data['venues']


def test_a_corrupt_config_raises_rather_than_reading_as_empty(tmp_path):
    """An empty read would start the system flat and report everything at the
    venue as an orphan."""
    path = tmp_path / 'config.json'
    path.write_text('{ this is not json')
    with pytest.raises(json.JSONDecodeError):
        TraderConfig.from_file(str(path))


def test_a_missing_config_is_defaults_not_an_error(tmp_path):
    cfg = TraderConfig.from_file(str(tmp_path / 'nothing.json'))
    assert cfg.settings['PRICE_REFRESH_SEC'] == 0.5
    assert cfg.contracts == {}


def test_structural_settings_are_named_so_the_ui_can_say_restart():
    cfg = TraderConfig()
    changed = cfg.structural_changes({'PRICE_REFRESH_SEC': 0.25,
                                      'MAX_QUOTE_AGE_SEC': 20})
    assert changed == ['PRICE_REFRESH_SEC']


def test_writing_one_env_value_leaves_the_others_alone(tmp_path, monkeypatch):
    env = tmp_path / '.env'
    env.write_text('OTHER=keepme\nFIX_A=old\n')
    write_env_value('FIX_A', 'new', str(env))
    text = env.read_text()
    assert 'OTHER=keepme' in text and 'FIX_A=new' in text and 'old' not in text
    write_env_value('FIX_B', 'added', str(env))
    assert 'FIX_B=added' in env.read_text()
    assert 'OTHER=keepme' in env.read_text()
