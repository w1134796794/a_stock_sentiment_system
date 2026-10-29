# Minute Candle Time Contract

- Canonical candles use end labels: 09:31 through 11:30, then 13:01 through
  15:00 (240 candles). Neither 09:30 nor 13:00 is a required candle.
- Preserve eltdx native timestamps: its native one-minute first candle is
  09:31. Do not request `bar_time="end"`, which adds another period. Native
  one-minute candles are declared end-labelled for strategy availability;
  other periods remain unknown until independently verified. Known start-labelled
  input from other contracts is shifted once, preserving `source_time`.
  Legacy input without a declared label is not silently shifted.
- An end-labelled candle can be used at its labelled minute once available.
  Unknown legacy labels retain conservative next-minute availability.
- Market time and receipt time are distinct. A morning candle received at
  13:00 keeps its morning market time; a 13:00 market label is not assumed
  to mean 11:30. Timestamped reads exclude receipts from the future.
- Minute diagnostics use the same availability rule. Historical bars without
  arrival evidence do not establish that a live process received them on time.
- Opening structure uses real minute OHLC. The real session opening price is
  still required independently, and missing candles are not fabricated.
