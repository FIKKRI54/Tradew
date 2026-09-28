"""Honest prospective-outcome summary from explicitly bound broker positions."""
from setup_tracker import SetupTracker


def prospective_report(root, minimum_outcomes=100):
    setups = list(SetupTracker(root).state['setups'].values())
    closed = [setup for setup in setups if setup.get('state') == 'CLOSED' and
              isinstance(setup.get('outcome', {}).get('net_result'), (int, float))]
    values = [setup['outcome']['net_result'] for setup in closed]
    sample = len(values)
    groups = {}
    for setup in closed:
        key = '|'.join((setup.get('family') or 'unknown',
                        setup.get('volatility_regime') or 'unknown'))
        group = groups.setdefault(key, {'sample_size': 0, 'positive_outcomes': 0,
                                        'net_result': 0})
        group['sample_size'] += 1
        group['positive_outcomes'] += setup['outcome']['net_result'] > 0
        group['net_result'] += setup['outcome']['net_result']
    return {
        'status': 'RESEARCH_ONLY' if sample < minimum_outcomes else 'REVIEW_READY',
        'sample_size': sample, 'minimum_review_sample': minimum_outcomes,
        'positive_outcomes': sum(value > 0 for value in values),
        'positive_rate': sum(value > 0 for value in values)/sample if sample else None,
        'net_result': sum(values),
        'mean_result': sum(values)/sample if sample else None,
        'by_setup_and_regime': groups,
        'scope': 'Only explicitly bound positions reconciled to broker exit deals.',
        'warning': ('This is prospective operational evidence, not a calibrated win probability. '
                    'Review costs, missing bindings, and regime sample sizes before inference.'),
    }
