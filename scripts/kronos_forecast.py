import os
import sys

import pandas as pd
import requests


def fetch_btc_ohlcv(lookback: int = 400) -> pd.DataFrame:
    url = "https://api.binance.com/api/v3/klines"
    params = {"symbol": "BTCUSDT", "interval": "1h", "limit": lookback}
    resp = requests.get(url, params=params, timeout=15)
    resp.raise_for_status()
    klines = resp.json()
    df = pd.DataFrame(
        klines,
        columns=[
            "timestamps", "open", "high", "low", "close", "volume",
            "close_time", "quote_volume", "trades",
            "taker_buy_base", "taker_buy_quote", "ignore",
        ],
    )
    df["timestamps"] = pd.to_datetime(df["timestamps"].astype(int), unit="ms", utc=True)
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)
    # Binance's quote_volume is the currency-denominated turnover (≈ amount)
    df["amount"] = df["quote_volume"].astype(float)
    return df[["timestamps", "open", "high", "low", "close", "volume", "amount"]]


def run_kronos_forecast(pred_len: int = 24) -> dict:
    kronos_path = os.environ.get("KRONOS_REPO_PATH", "kronos_repo")
    if kronos_path not in sys.path:
        sys.path.insert(0, kronos_path)

    from model import Kronos, KronosPredictor, KronosTokenizer  # noqa: PLC0415

    model_id = os.environ.get("KRONOS_MODEL_ID", "NeoQuasar/Kronos-mini")
    tokenizer_id = os.environ.get("KRONOS_TOKENIZER_ID", "NeoQuasar/Kronos-Tokenizer-base")

    print("Fetching BTC/USDT hourly OHLCV data from Binance...")
    df = fetch_btc_ohlcv(lookback=400)

    print(f"Loading Kronos model ({model_id})...")
    tokenizer = KronosTokenizer.from_pretrained(tokenizer_id)
    model = Kronos.from_pretrained(model_id)
    predictor = KronosPredictor(model, tokenizer, max_context=400)

    x_df = df[["open", "high", "low", "close", "volume", "amount"]]
    x_timestamp = df["timestamps"]

    last_ts = df["timestamps"].iloc[-1]
    y_timestamp = pd.Series(
        pd.date_range(
            start=last_ts + pd.Timedelta(hours=1),
            periods=pred_len,
            freq="1h",
            tz="UTC",
        )
    )

    print(f"Running Kronos forecast for the next {pred_len} hours...")
    pred_df = predictor.predict(
        df=x_df,
        x_timestamp=x_timestamp,
        y_timestamp=y_timestamp,
        pred_len=pred_len,
        T=1.0,
        top_p=0.9,
        sample_count=1,
    )

    current_close = float(df["close"].iloc[-1])
    predicted_close = float(pred_df["close"].iloc[-1])
    change_pct = (predicted_close - current_close) / current_close * 100
    direction = "bullish" if change_pct > 0 else "bearish"

    result = {
        "symbol": "BTC/USDT",
        "current_price": current_close,
        "predicted_price": predicted_close,
        "change_pct": change_pct,
        "direction": direction,
        "pred_horizon_hours": pred_len,
    }
    print(f"Forecast result: {result}")
    return result


def build_video_entry(forecast: dict) -> dict:
    direction = forecast["direction"]
    change_pct = abs(forecast["change_pct"])

    if direction == "bullish":
        visual = "rising green candlestick chart with golden upward momentum and buy signals"
        mood = "optimistic upward surge"
    else:
        visual = "declining red candlestick chart with downward pressure and cool blue tones"
        mood = "cautious bearish decline"

    prompt = (
        f"Financial trading terminal showing BTC/USDT {visual}, "
        f"AI forecast overlay predicting a {mood}, cinematic close-up macro shot with depth of field"
    )
    topic = f"BTC 24h Forecast: {direction.capitalize()} ({change_pct:.1f}%)"
    return {"prompt": prompt, "topic": topic}
