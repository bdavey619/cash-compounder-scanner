"""Build an offline fixtures folder in the same formats SEC EDGAR and Stooq return.

All companies here are fictional ("Sample ... Corp", tickers SMP*), with numbers chosen so
each one exercises a different branch of the screen. Run:  python tests/make_fixtures.py fixtures/
"""
import json
import math
import random
import sys
from datetime import date, timedelta
from pathlib import Path

ASOF = date(2026, 11, 6)

# ticker, name, cik, fcf history ($B, 3 FYs oldest first), fcf margin, roc capital ($B),
# price/FCF multiple, 5-day move, insider buyers (list of (name, role, code)), net cash $B
COMPANIES = [
    ("SMPA", "Sample Alpha Software Corp", 9000001, [1.00, 1.30, 1.70], 0.32, 6.0, 16, -0.14, [], 2.0),
    ("SMPB", "Sample Beta Payments Inc", 9000002, [2.00, 2.40, 2.90], 0.41, 12.0, 15, -0.03,
        [("Jane Ortiz", "Chief Financial Officer", "P"), ("Raj Mehta", "Director", "P"), ("Lee Park", "Director", "P")], 1.0),
    ("SMPC", "Sample Gamma Chips Inc", 9000003, [1.30, 1.40, 1.50], 0.30, 5.0, 70, -0.12,
        [("Sam Cole", "CEO", "S")], 0.5),
    ("SMPD", "Sample Delta Networks Co", 9000004, [0.80, 2.40, 2.10], 0.28, 9.0, 13, -0.01, [], 0.0),
    ("SMPE", "Sample Epsilon Retail Co", 9000005, [0.40, 0.42, 0.45], 0.04, 6.0, 20, -0.15, [], 0.0),
    ("SMPF", "Sample Phi Biotech Inc", 9000006, [-0.20, 0.30, 0.60], 0.25, 3.0, 25, -0.20, [], 1.0),
    ("SMPG", "Sample Gamma-Ray Media Co", 9000007, [0.90, 1.00, 1.10], 0.22, 5.0, 28, 0.02, [], 0.0),
    ("SMPH", "Sample Tiny Tools Inc", 9000008, [0.050, 0.060, 0.070], 0.30, 0.3, 15, -0.18, [], 0.0),
]
FY_ENDS = ["2023-12-31", "2024-12-31", "2025-12-31"]


def fact(start, end, val, fy, filed, form="10-K"):
    d = {"end": end, "val": val, "accn": f"0009999999-{fy%100:02d}-000001", "fy": fy, "fp": "FY", "form": form, "filed": filed}
    if start:
        d["start"] = start
    return d


def company_facts(cik, name, fcfs, margin, capital_bn, net_cash_bn, shares):
    ocf, capex, rev = [], [], []
    for fy_report in (2024, 2025):          # two 10-Ks, each restating prior years (as real filings do)
        filed = f"{fy_report+1}-02-15"
        for i, end in enumerate(FY_ENDS):
            if int(end[:4]) > fy_report:
                continue
            start = f"{int(end[:4])}-01-01"
            f = fcfs[i] * 1e9
            cx = abs(f) * 0.15 + 0.05e9
            ocf.append(fact(start, end, f + cx, fy_report, filed))
            capex.append(fact(start, end, cx, fy_report, filed))
            rev.append(fact(start, end, abs(fcfs[-1]) * 1e9 / margin * (1 - 0.08 * (2 - i)), fy_report, filed))
    # 10-Q year-to-date facts (9 months), this year and last year, as the Q3 2026 10-Q reports them
    f25 = fcfs[-1] * 1e9
    cx25 = abs(f25) * 0.15 + 0.05e9
    rev25 = abs(fcfs[-1]) * 1e9 / margin
    q = lambda start, end, val: {"start": start, "end": end, "val": val, "accn": "q3", "fy": 2026, "fp": "Q3", "form": "10-Q", "filed": "2026-10-30"}
    for lst, full in ((ocf, f25 + cx25), (capex, cx25), (rev, rev25)):
        lst.append(q("2025-01-01", "2025-09-30", full * 0.75))
        lst.append(q("2026-01-01", "2026-09-30", full * 0.81))
    # a stale Q2 fact that must not be used (the Q3 YTD is newer)
    ocf.append({"start": "2026-01-01", "end": "2026-06-30", "val": 9.99e12, "accn": "x", "fy": 2026, "fp": "Q2", "form": "10-Q", "filed": "2026-08-01"})
    inst = lambda v, end="2025-12-31": {"USD": [fact(None, end, v, 2025, "2026-02-15")]}
    return {
        "cik": cik, "entityName": name,
        "facts": {
            "dei": {"EntityCommonStockSharesOutstanding": {"label": "Shares", "units": {"shares": [
                {"end": "2026-10-15", "val": shares, "accn": "y", "fy": 2026, "fp": "Q3", "form": "10-Q", "filed": "2026-10-30"}]}}},
            "us-gaap": {
                "NetCashProvidedByUsedInOperatingActivities": {"label": "OCF", "units": {"USD": ocf}},
                "PaymentsToAcquirePropertyPlantAndEquipment": {"label": "Capex", "units": {"USD": capex}},
                "RevenueFromContractWithCustomerExcludingAssessedTax": {"label": "Revenue", "units": {"USD": rev}},
                "StockholdersEquity": {"label": "Equity", "units": inst(capital_bn * 0.7e9)},
                "LongTermDebt": {"label": "LTD", "units": inst(capital_bn * 0.3e9)},
                "CashAndCashEquivalentsAtCarryingValue": {"label": "Cash", "units": inst((net_cash_bn + capital_bn * 0.3) * 1e9)},
            },
        },
    }


def prices(target_last, move5, seed):
    rnd = random.Random(seed)
    days, d = [], ASOF
    while len(days) < 260:
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    days.reverse()
    pre = target_last / (1 + move5)
    px = [pre * 0.85]
    for _ in range(len(days) - 7):
        px.append(px[-1] * (1 + rnd.gauss(0.0007, 0.012)))
    scale = pre / px[-1]
    px = [p * scale for p in px]
    px.append(pre)
    for k in range(1, 6):
        px.append(pre * (1 + move5) ** (k / 5) * (1 + rnd.gauss(0, 0.002)))
    px[-1] = target_last
    lines = ["Date,Open,High,Low,Close,Volume"]
    for day, p in zip(days, px[-len(days):]):
        lines.append(f"{day.isoformat()},{p:.2f},{p*1.01:.2f},{p*0.99:.2f},{p:.2f},1000000")
    return "\n".join(lines) + "\n"


def form4_xml(issuer_cik, ticker, issuer_name, owner, role, code, day, shares, price):
    is_dir = "1" if role == "Director" else "0"
    is_off = "0" if role == "Director" else "1"
    title = "" if role == "Director" else f"<officerTitle>{role}</officerTitle>"
    return f"""<SEC-DOCUMENT>
<DOCUMENT><TYPE>4<TEXT><XML>
<?xml version="1.0"?>
<ownershipDocument>
 <schemaVersion>X0508</schemaVersion><documentType>4</documentType><periodOfReport>{day}</periodOfReport>
 <issuer><issuerCik>{issuer_cik:010d}</issuerCik><issuerName>{issuer_name}</issuerName><issuerTradingSymbol>{ticker}</issuerTradingSymbol></issuer>
 <reportingOwner><reportingOwnerId><rptOwnerCik>0008000001</rptOwnerCik><rptOwnerName>{owner}</rptOwnerName></reportingOwnerId>
  <reportingOwnerRelationship><isDirector>{is_dir}</isDirector><isOfficer>{is_off}</isOfficer>{title}</reportingOwnerRelationship></reportingOwner>
 <nonDerivativeTable><nonDerivativeTransaction>
  <securityTitle><value>Common Stock</value></securityTitle><transactionDate><value>{day}</value></transactionDate>
  <transactionCoding><transactionFormType>4</transactionFormType><transactionCode>{code}</transactionCode></transactionCoding>
  <transactionAmounts><transactionShares><value>{shares}</value></transactionShares><transactionPricePerShare><value>{price:.2f}</value></transactionPricePerShare><transactionAcquiredDisposedCode><value>{'A' if code=='P' else 'D'}</value></transactionAcquiredDisposedCode></transactionAmounts>
 </nonDerivativeTransaction></nonDerivativeTable>
</ownershipDocument>
</XML></TEXT></DOCUMENT></SEC-DOCUMENT>
"""


def main(root):
    root = Path(root)
    for sub in ("companyfacts", "prices", "daily-index", "archives"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    tickers = {}
    idx_lines = {}
    for n, (tk, name, cik, fcfs, margin, cap_bn, mult, move, buyers, net) in enumerate(COMPANIES):
        tickers[str(n)] = {"cik_str": cik, "ticker": tk, "title": name}
        mcap = fcfs[-1] * 1e9 * mult
        price = 50 + n * 37.5
        shares = mcap / price
        (root / "companyfacts" / f"CIK{cik:010d}.json").write_text(json.dumps(company_facts(cik, name, fcfs, margin, cap_bn, net, shares)))
        (root / "prices" / f"{tk.lower()}.csv").write_text(prices(price, move, cik))
        for j, (owner, role, code) in enumerate(buyers):
            day = ASOF - timedelta(days=j + 1 if (ASOF - timedelta(days=j + 1)).weekday() < 5 else j + 3)
            ymd = day.strftime("%Y%m%d")
            accn = f"0001234567-26-{cik % 1000:03d}{j:03d}"
            path = f"edgar/data/{cik}/{accn}.txt"
            (root / "archives" / Path(path).parent).mkdir(parents=True, exist_ok=True)
            (root / "archives" / path).write_text(form4_xml(cik, tk, name, owner, role, code, day.isoformat(), 2000 + 500 * j, price))
            idx_lines.setdefault(ymd, []).append(f"{'4':<12}{name[:60]:<62}{cik:<12}{ymd:<12}{path}")
            # the same filing is also listed under the reporting owner's CIK (as on EDGAR)
            idx_lines[ymd].append(f"{'4':<12}{owner[:60]:<62}{8000001:<12}{ymd:<12}{path}")
    # an unrelated 10-Q line and a noise Form 4 for a company outside the universe
    for ymd, lines in idx_lines.items():
        lines.append(f"{'10-Q':<12}{'Unrelated Holdings Inc':<62}{1111111:<12}{ymd:<12}edgar/data/1111111/0000000000-26-000001.txt")
    header = ("Description:           Daily Index of EDGAR Dissemination Feed by Form Type\n"
              "Last Data Received:    November 6, 2026\n\n"
              "Form Type   Company Name                                                  CIK         Date Filed  File Name\n"
              + "-" * 140 + "\n")
    for ymd, lines in idx_lines.items():
        (root / "daily-index" / f"form.{ymd}.idx").write_text(header + "\n".join(lines) + "\n")
    (root / "company_tickers.json").write_text(json.dumps(tickers))
    print(f"wrote fixtures for {len(COMPANIES)} sample companies to {root}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "fixtures")
