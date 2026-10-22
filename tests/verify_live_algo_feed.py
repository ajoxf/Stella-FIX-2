"""Read-only TT UAT smoke check: python tests/verify_live_algo_feed.py."""
import os
import argparse
import sys
import time

from dotenv import load_dotenv
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from fixtrader.config import TraderConfig  # noqa: E402
from fixtrader.gateway import FixGateway  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--security-id')
    parser.add_argument('--symbol')
    parser.add_argument('--exchange', default='')
    args = parser.parse_args()
    load_dotenv()
    config = TraderConfig.from_file('config.tt-uat.json')
    contracts = list(config.enabled_contracts())
    if args.security_id and args.symbol:
        contracts = [SimpleNamespace(key='verification', name=args.symbol,
                                     symbol=args.symbol, security_id=args.security_id,
                                     security_exchange=args.exchange,
                                     venue=contracts[0].venue)]
    venue = config.venues[contracts[0].venue]
    gateway = FixGateway(venue, contracts)
    try:
        gateway.start()
        for contract in contracts:
            gateway.subscribe(contract)
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            gateway.drain_events()
            for event in gateway.drain_market_data():
                if event.bid is not None and event.ask is not None:
                    print('LIVE_EVENT', event.contract_key, event.security_id,
                          event.received_at.isoformat(), event.bid, event.ask,
                          event.last, flush=True)
                    return 0
            if gateway.state().value == 'ERROR':
                print('FIX_ERROR', gateway.state_text(), flush=True)
                return 2
            time.sleep(0.1)
        print('NO_LIVE_EVENT', gateway.state_text(),
              'subscriptions=', len(gateway.terminal.subscriptions),
              'books=', len(gateway.terminal.books),
              'errors=', gateway.terminal.errors[-3:], flush=True)
        return 1
    finally:
        gateway.stop()


if __name__ == '__main__':
    raise SystemExit(main())
