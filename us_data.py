import pandas as pd
import yfinance as yf


def get_us_eps_estimate(ticker):
    try:
        t = yf.Ticker(ticker)
        df = t.earnings_estimate
        if df is None or (isinstance(df, pd.DataFrame) and df.empty):
            return pd.DataFrame()
        return df
    except Exception:
        return pd.DataFrame()


def get_us_revenue_estimate(ticker):
    try:
        t = yf.Ticker(ticker)
        df = t.revenue_estimate
        if df is None or (isinstance(df, pd.DataFrame) and df.empty):
            return pd.DataFrame()
        return df
    except Exception:
        return pd.DataFrame()


def get_us_eps_revisions(ticker):
    try:
        t = yf.Ticker(ticker)
        df = t.eps_revisions
        if df is None or (isinstance(df, pd.DataFrame) and df.empty):
            return pd.DataFrame()
        return df
    except Exception:
        return pd.DataFrame()


def get_us_price_target(ticker):
    try:
        t = yf.Ticker(ticker)
        pt = t.analyst_price_targets
        if pt and isinstance(pt, dict) and pt.get("mean") is not None:
            return {
                "current": pt.get("current"),
                "mean": pt.get("mean"),
                "median": pt.get("median"),
                "high": pt.get("high"),
                "low": pt.get("low"),
            }
    except Exception:
        pass

    try:
        t = yf.Ticker(ticker)
        info = t.info or {}
        mean_p = info.get("targetMeanPrice")
        if mean_p is None:
            return {}
        return {
            "current": info.get("currentPrice"),
            "mean": mean_p,
            "median": info.get("targetMedianPrice"),
            "high": info.get("targetHighPrice"),
            "low": info.get("targetLowPrice"),
            "numberOfAnalysts": info.get("numberOfAnalystOpinions"),
        }
    except Exception:
        return {}


if __name__ == "__main__":
    pd.set_option("display.max_columns", None)
    pd.set_option("display.width", 200)

    print("=== AAPL EPS estimate ===")
    print(get_us_eps_estimate("AAPL"))
    print()
    print("=== AAPL Revenue estimate ===")
    print(get_us_revenue_estimate("AAPL"))
    print()
    print("=== AAPL EPS revisions ===")
    print(get_us_eps_revisions("AAPL"))
    print()
    print("=== AAPL Price target ===")
    print(get_us_price_target("AAPL"))
