"""QBench: the quant workstation inside Autobench.

  stats        Sharpe with sampling error (PSR, Deflated Sharpe, MinTRL), ADF/Engle-Granger, variance ratio,
               Hurst, half-life, OLS, Ledoit-Wolf
  strategies   the strategy library (rules, papers, evidence, failure modes) + single-asset position functions
  lab          backtest one strategy; walk-forward-optimise every library strategy on a symbol
  profile      quant profile: return distribution, volatility regime, trending vs mean-reverting tests,
               factor-ETF exposures, seasonality, overnight vs intraday
  factors      cross-sectional factor ranks over the ~6,000-stock universe (also screener fields q_*)
  xsection     factor-ranking backtests over a list (watchlist)
  pairs        cointegration pair scan and rolling pair backtest
  hedge        risk-based allocation (inverse vol, ERC, min variance, HRP) and beta / min-variance hedges
  confluence   TA + FA + quant in one score with quant-derived levels and sizing; watchlist ranking
"""
