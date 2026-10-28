"""Availability of stored candles; receipt time never changes the market label."""
import pandas as pd


def end_label_minutes(frame):
    data = frame.copy()
    if 'bar_label' not in data:
        return data
    start = data['bar_label'].eq('start')
    if start.any():
        data['source_time'] = data.get('source_time', data['time'])
        data.loc[start, 'time'] = (pd.to_datetime(data.loc[start, 'time'], format='%H:%M:%S')
                                   + pd.Timedelta(minutes=1)).dt.strftime('%H:%M:%S')
        data.loc[start, 'bar_label'] = 'end'
    return data


def completed_minutes(frame, at):
    if frame is None or frame.empty:
        return frame
    frame = end_label_minutes(frame)
    text = str(at).replace('T', ' ').split(' ')[-1][:8]
    cutoff = text[:5] + ':00'
    labels = frame.get('bar_label', pd.Series('unknown', index=frame.index))
    mask = frame['time'].lt(cutoff) | (labels.eq('end') & frame['time'].le(cutoff))
    if 'received_at' in frame:
        receipts = pd.to_datetime(frame['received_at'], errors='coerce', utc=True)
        # Callers with only a time string cannot infer a receipt date.
        if len(str(at)) > 8:
            stamp = pd.Timestamp(at)
            if stamp.tzinfo is None:
                stamp = stamp.tz_localize('Asia/Shanghai')
            mask &= receipts.isna() | receipts.le(stamp.tz_convert('UTC'))
    return frame[mask].copy()
