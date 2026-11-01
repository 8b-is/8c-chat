#!/usr/bin/env python3
"""Shared Stripe contribution reconciliation and conservative hosted allocations.

Amounts are integer micro-USD (1 USD = 1,000,000). Stripe amounts are USD cents.
80% of paid, unrefunded contributions is allocated to hosted inference; 20% is
held for payment fees/operating margin. Provider-client costs are estimates, not
a replacement for the provider's billing ledger. Unknown outcomes retain their
reservation. No card data or donor identity is written to the commons.
"""
from fractions import Fraction
import json
import os
from pathlib import Path
import time
import urllib.parse
import urllib.request
from worker import save

RESERVATION = 1_000_000


def micro_usd(value):
    amount = Fraction(str(value)) * 1_000_000
    if amount < 0:
        raise ValueError('Invalid provider cost')
    return (amount.numerator + amount.denominator - 1) // amount.denominator


def stripe_total(sessions, link):
    cents = 0
    seen = set()
    for session in sessions:
        if session['id'] in seen:
            raise ValueError('Duplicate Stripe session')
        seen.add(session['id'])
        if session.get('payment_link') != link or not session.get('livemode'):
            raise ValueError('Stripe source mismatch')
        if session.get('payment_status') != 'paid':
            continue
        charge = session.get('payment_intent', {}).get('latest_charge', {})
        total, refunded = session.get('amount_total'), charge.get('amount_refunded')
        if session.get('currency') != 'usd' or type(total) is not int or type(refunded) is not int or not 0 <= refunded <= total:
            raise ValueError('Unsupported contribution currency or charge state')
        if not charge.get('paid') or charge.get('disputed'):
            continue
        cents += total - refunded
    return cents


def sync(secret, link, destination):
    sessions = []
    cursor = ''
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for _ in range(32):
        query = {'payment_link': link, 'limit': 100, 'expand[]': 'data.payment_intent.latest_charge'}
        if cursor:
            query['starting_after'] = cursor
        req = urllib.request.Request('https://api.stripe.com/v1/checkout/sessions?' + urllib.parse.urlencode(query),
                                     headers={'Authorization': 'Bearer ' + secret})
        with opener.open(req, timeout=15) as response:
            raw = response.read(4_194_305)
            if len(raw) > 4_194_304:
                raise ValueError('Stripe page exceeds bounds')
            page = json.loads(raw)
        sessions += page['data']
        if not page['has_more']:
            cents = stripe_total(sessions, link)
            result = {'version': 1, 'checked_at': time.time_ns() // 1_000_000_000,
                      'paid_unrefunded_cents': cents, 'allocated_micro_usd': (cents * 80 // 100) * 10_000,
                      'scope': '8c-shared-all-participants-all-rooms'}
            save(Path(destination), result)
            return result
        cursor = page['data'][-1]['id']
    raise ValueError('Contribution pagination requires expansion; old snapshot retained')


class Pool:
    def __init__(self, snapshot, directory):
        self.snapshot = Path(snapshot) if snapshot else None
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    def available(self):
        if not self.snapshot or not self.snapshot.exists():
            return 0
        funding = json.loads(self.snapshot.read_text())
        age = time.time_ns() // 1_000_000_000 - funding['checked_at']
        if funding['version'] != 1 or not 0 <= age <= 300:
            return 0
        receipts = list(self.directory.glob('*.json'))
        if len(receipts) > 20_000:
            raise ValueError('Archive funding receipts before expanding capacity')
        spent = sum(json.loads(p.read_text())['held_micro_usd'] for p in receipts)
        return max(0, funding['allocated_micro_usd'] - spent)

    def reserve(self, key):
        path = self.directory / (key + '.json')
        if path.exists() or self.available() < RESERVATION:
            return False
        save(path, {'state': 'reserved', 'held_micro_usd': RESERVATION})
        return True

    def settle(self, key, estimate):
        # Zero/missing cost does not establish a free paid-provider request.
        cost = micro_usd(estimate) if estimate is not None else 0
        save(self.directory / (key + '.json'), {'state': 'estimated' if cost else 'unreconciled',
             'held_micro_usd': cost or RESERVATION, 'provider_estimate_micro_usd': cost})


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--link', required=True)
    parser.add_argument('--snapshot', required=True)
    args = parser.parse_args()
    sync(os.environ['STRIPE_SECRET_KEY'], args.link, args.snapshot)
