---
name: fundamental-analysis
description: Fundamental analysis of a US company — business quality, growth, profitability, balance sheet, cash-flow quality, capital allocation, valuation vs history and peers, earnings estimates, and SEC filing review — using Autobench tools. Use when the user asks about valuation, financials, earnings, "is it cheap/expensive", business quality, competitors, or long-term investment merit.
---

# Fundamental analysis

Think like an owner: what does the business earn, how durably, how fast is it growing, what are you
paying for it, and what could break the thesis?

## Data map
| Need | Tool |
|---|---|
| Snapshot (multiples, margins, growth, leverage, consensus) | `get_fundamentals` |
| Last 4-5 years/quarters + derived margins/growth/coverage/dilution | `get_financial_statements` (income, balance, cashflow; annual & quarterly) |
| 10-15 year as-reported history (cycles, long CAGR) | `get_financial_history` (SEC XBRL) |
| Beats/misses, estimates, revisions | `get_earnings` |
| Peers side by side + medians + performance | `compare_peers([...])` (pick 3-6 true competitors yourself) |
| Filings text | `list_sec_filings` → `read_sec_filing(url, section=...)`; 8-K exhibits via `get_filing_documents` |

Yahoo snapshot data can be stale or mis-mapped. For any number that drives the conclusion, cross-check
against `get_financial_history` / statements and say which source you used.

## Checklist
1. **Business & moat** — what they sell, segments, customers, pricing power. From profile + 10-K
   Item 1 (Business) when the horizon is position/long-term.
2. **Growth** — revenue & EPS growth (yoy quarterly, 3-5y CAGR), acceleration/deceleration, estimates for
   0y/+1y, organic vs acquired (check cash-flow acquisitions).
3. **Profitability** — gross/operating/net margins trend, ROE/ROA; operating leverage (margins rising
   with revenue?).
4. **Cash-flow quality** — FCF vs net income (conversion ≥ ~80% is healthy), SBC as % of OCF (high
   SBC inflates FCF), capex intensity, working-capital swings.
5. **Balance sheet** — net debt/EBITDA, debt/equity, current ratio, interest coverage; refinancing walls
   (10-K debt note) if leverage is high.
6. **Capital allocation** — buybacks vs dilution (share count change), dividends & payout, M&A record.
7. **Valuation** — current multiples vs (a) own history (compute from `get_financial_history` + price
   history) and (b) peer medians. Match multiple to business: EV/EBITDA or P/E for profitable,
   EV/Sales or gross-profit multiples for high-growth, P/B for banks, P/FFO for REITs.
   Quick reverse-DCF sanity check: what FCF growth does the current FCF yield imply? (FCF yield ≈
   discount rate − growth for a steady grower; at 10% discount, a 2% FCF yield implies ~8% perpetual growth.)
8. **Estimates & revisions** — `eps_trend` 7/30/90-day changes and `eps_revisions` up vs down: rising
   estimates are one of the most reliable fundamental tailwinds. Beat rate and average surprise.
9. **Risks from filings** — read Item 1A risk factors for *new or changed* risks (compare with prior
   year if important), Item 7 MD&A for guidance, demand commentary, margin drivers; Item 3 legal.
10. **Red flags** — receivables or inventory growing much faster than revenue; FCF ≪ net income
    persistently; rising share count; frequent "adjusted" metrics; auditor/CFO changes (8-K 4.01/5.02);
    going-concern language; goodwill > equity; customer concentration; restatements (10-K/A).

## Scoring (fundamental pillar, -100..100)
Quality (margins, returns, FCF conversion) 30% · Growth & revisions 30% · Balance sheet 20% ·
Capital allocation 10% · Red flags −10% to −50%. Valuation is scored separately (cheap vs history
and peers given quality/growth = positive).

## Output block
```
Business: … | Growth: rev +x% yoy (3y CAGR y%), EPS … | Est. revisions: up/down (30d)
Margins: GM … OM … (trend) | FCF conv … | SBC/OCF … | Net debt/EBITDA … | Share count …
Valuation: fwd P/E … vs peers … vs 5y avg … | EV/EBITDA … | FCF yield … → cheap/fair/rich
Filings: notable risks / guidance … | Red flags: none / …
Fundamental score: … | Valuation score: …
```
