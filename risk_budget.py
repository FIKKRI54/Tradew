"""Shared cash-risk budgets derived from the current account snapshot."""


INDEPENDENT_RISK_FRACTIONS = {
    'LOW': 0.10,
    'MEDIUM': 0.20,
    'HIGH': 0.30,
}


def independent_risk_fraction(tier):
    """Return the immutable independent-mode risk fraction for an explicit tier."""
    try:
        return INDEPENDENT_RISK_FRACTIONS[tier]
    except (KeyError, TypeError):
        raise ValueError('Independent plan requires LOW, MEDIUM, or HIGH risk tier') from None


def cash_risk_budgets(snapshot, config):
    fraction = config.get('balance_risk_fraction')
    balance = (snapshot.get('account') or {}).get('balance')
    if isinstance(fraction, (int, float)) and isinstance(balance, (int, float)):
        budget = balance * fraction
        return budget, budget, 'account_balance_fraction'
    return (config['per_trade_cash_budget'], config['portfolio_cash_budget'],
            'fixed_cash_budget')
